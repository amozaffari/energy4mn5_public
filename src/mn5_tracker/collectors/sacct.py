"""Incremental pull and parsing of the user's own Slurm job allocations (`sacct -X`)."""

from dataclasses import dataclass
from datetime import date

from mn5_tracker.collectors import ParseError


SACCT_FIELDS = (
    "JobID",
    "JobName%80",
    "Account",
    "Partition",
    "QOS",
    "State",
    "Submit",
    "Start",
    "End",
    "ElapsedRaw",
    "NNodes",
    "AllocCPUS",
    "AllocTRES%200",
    "CPUTimeRAW",
    "ExitCode",
    "WorkDir%300",
    "ConsumedEnergyRaw",
)
TERMINAL_STATES = frozenset(
    {
        "BOOT_FAIL",
        "CANCELLED",
        "COMPLETED",
        "DEADLINE",
        "FAILED",
        "NODE_FAIL",
        "OUT_OF_MEMORY",
        "PREEMPTED",
        "TIMEOUT",
    }
)
UNKNOWN_TIMESTAMPS = frozenset({"", "Unknown", "None"})


@dataclass(frozen=True)
class SacctJob:
    job_id: str
    job_name: str
    account: str
    partition: str
    qos: str
    state: str
    state_raw: str
    submit: str | None
    start: str | None
    end: str | None
    elapsed_s: int
    n_nodes: int
    alloc_cpus: int
    alloc_tres: str
    cpu_time_raw_s: int
    exit_code: str
    work_dir: str
    consumed_energy_j: int | None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def tres(self) -> dict[str, str]:
        return parse_tres(self.alloc_tres)


def build_sacct_command(user: str, since: date) -> str:
    return (
        f"sacct -u {user} -X -n -P -S {since.isoformat()} -E now --format={','.join(SACCT_FIELDS)}"
    )


def parse_tres(alloc_tres: str) -> dict[str, str]:
    """`billing=160,cpu=160,gres/gpu=4,node=1` → {"billing": "160", ...}."""
    pairs = (item.split("=", 1) for item in alloc_tres.split(",") if "=" in item)

    return {key: value for key, value in pairs}


def parse_int(value: str) -> int:
    return int(value) if value.strip().isdigit() else 0


def parse_timestamp(value: str) -> str | None:
    return None if value in UNKNOWN_TIMESTAMPS else value


def parse_energy(value: str) -> int | None:
    """MN5 fills ConsumedEnergyRaw for GPP jobs only; 0 or blank means "not measured"."""
    energy = parse_int(value)

    return energy or None


def parse_sacct_line(line: str, line_number: int) -> SacctJob:
    fields = line.split("|")

    if len(fields) != len(SACCT_FIELDS):
        raise ParseError(
            f"sacct line {line_number}: expected {len(SACCT_FIELDS)} fields, got {len(fields)} "
            f"(a '|' inside JobName or WorkDir?): {line[:200]!r}"
        )

    (
        job_id,
        job_name,
        account,
        partition,
        qos,
        state_raw,
        submit,
        start,
        end,
        elapsed_raw,
        n_nodes,
        alloc_cpus,
        alloc_tres,
        cpu_time_raw,
        exit_code,
        work_dir,
        consumed_energy_raw,
    ) = fields

    # "CANCELLED by 1000" carries the canceller's uid; the base state is what we aggregate on.
    state = state_raw.split(" ", 1)[0]

    return SacctJob(
        job_id=job_id,
        job_name=job_name,
        account=account,
        partition=partition,
        qos=qos,
        state=state,
        state_raw=state_raw,
        submit=parse_timestamp(submit),
        start=parse_timestamp(start),
        end=parse_timestamp(end),
        elapsed_s=parse_int(elapsed_raw),
        n_nodes=parse_int(n_nodes),
        alloc_cpus=parse_int(alloc_cpus),
        alloc_tres=alloc_tres,
        cpu_time_raw_s=parse_int(cpu_time_raw),
        exit_code=exit_code,
        work_dir=work_dir,
        consumed_energy_j=parse_energy(consumed_energy_raw),
    )


def parse_sacct(text: str) -> list[SacctJob]:
    """Parse `sacct -n -P` output; blank lines are skipped, malformed lines fail loudly."""
    jobs = []

    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue

        jobs.append(parse_sacct_line(line, line_number))

    return jobs
