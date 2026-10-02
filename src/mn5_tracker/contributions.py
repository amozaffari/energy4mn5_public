"""Share a member's own project usage as aggregated totals, and import other members' shares.

Each member can only see their own jobs, but keeps them forever, so the project total can be
rebuilt from everyone's contributions. A contribution holds totals per month × allocation ×
partition × state only: no job names, paths or job IDs.
"""

import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from mn5_tracker.config import Config
from mn5_tracker.energy import EnergyInput, is_exclusive
from mn5_tracker.store import JobRecord, Store, utc_now


CONTRIBUTION_SCHEMA = "mn5track.contribution/v1"
RECONCILIATION_WARN_PCT = 10.0
RECONCILIATION_MIN_KHOURS = 1.0


class ContributionError(ValueError):
    """Raised when a contribution file is not a valid mn5track contribution."""


@dataclass(frozen=True)
class ContributionRow:
    month: str
    account: str
    partition_class: str
    state: str
    jobs: int
    node_hours: float
    billed_node_hours: float
    gpu_hours_billed: float
    gpu_hours_requested: float
    core_hours_billed: float
    energy_kwh: float
    measured_billed_node_hours: float = 0.0
    queue: str = "production"


@dataclass(frozen=True)
class AccountTotal:
    """All of the contributor's usage on one allocation (any project), for cross-checking."""

    account: str
    partition_class: str
    khours_billed: float


@dataclass(frozen=True)
class Contribution:
    contributor: str
    project: str
    generated_at: str
    rules_fingerprint: str
    first_job: str | None
    last_job: str | None
    last_sync: str | None
    rows: tuple[ContributionRow, ...]
    account_totals: tuple[AccountTotal, ...]

    @property
    def jobs(self) -> int:
        return sum(row.jobs for row in self.rows)

    def billed_node_hours_by_class(self, account: str | None = None) -> dict[str, float]:
        by_class: dict[str, float] = defaultdict(float)

        for row in self.rows:
            if account is None or row.account == account:
                by_class[row.partition_class] += row.billed_node_hours

        return dict(by_class)

    def to_json(self) -> dict[str, Any]:
        return {"schema": CONTRIBUTION_SCHEMA, **asdict(self)}


@dataclass
class ImportOutcome:
    source: str
    contributor: str | None
    project: str | None
    status: str
    warnings: list[str]


def rounded_row(key: tuple[str, str, str, str, str], values: dict[str, float]) -> ContributionRow:
    month, account, partition_class, state, queue = key

    return ContributionRow(
        month=month,
        account=account,
        partition_class=partition_class,
        state=state,
        queue=queue,
        jobs=int(values["jobs"]),
        node_hours=round(values["node_hours"], 3),
        billed_node_hours=round(values["billed_node_hours"], 3),
        gpu_hours_billed=round(values["gpu_hours_billed"], 3),
        gpu_hours_requested=round(values["gpu_hours_requested"], 3),
        core_hours_billed=round(values["core_hours_billed"], 2),
        energy_kwh=round(values["energy_kwh"], 3),
        measured_billed_node_hours=round(values["measured_billed_node_hours"], 3),
    )


def build_contribution(
    config: Config, store: Store, jobs: list[JobRecord], project: str
) -> Contribution:
    project_jobs = [job for job in jobs if job.project == project and job.partition_class]
    totals: dict[tuple[str, str, str, str, str], dict[str, float]] = defaultdict(
        lambda: defaultdict(float)
    )

    for job in project_jobs:
        month = job.start_date.strftime("%Y-%m") if job.start_date else "not-started"
        values = totals[(month, job.account, job.partition_class or "", job.state, job.queue)]
        values["jobs"] += 1
        values["node_hours"] += job.node_hours
        values["billed_node_hours"] += job.billed_node_hours
        values["gpu_hours_billed"] += job.gpu_hours_billed
        values["gpu_hours_requested"] += job.gpu_hours_requested
        values["core_hours_billed"] += job.core_hours_billed

        # Only exclusive jobs' IPMI readings are their own; shared-node readings are modelled.
        if job.consumed_energy_j and is_exclusive(job, config):
            values["energy_kwh"] += job.consumed_energy_j / 3.6e6
            values["measured_billed_node_hours"] += job.billed_node_hours

    project_config = config.projects.get(project)
    relevant_accounts = set(project_config.allocations_hint if project_config else ()) | {
        job.account for job in project_jobs
    }
    account_khours: dict[tuple[str, str], float] = defaultdict(float)

    for job in jobs:
        if job.account in relevant_accounts and job.partition_class:
            account_khours[(job.account, job.partition_class)] += job.core_hours_billed / 1000

    starts = sorted(job.start for job in project_jobs if job.start)

    return Contribution(
        contributor=config.user,
        project=project,
        generated_at=utc_now(),
        rules_fingerprint=config.rules_fingerprint,
        first_job=starts[0] if starts else None,
        last_job=starts[-1] if starts else None,
        last_sync=store.last_successful_sync(),
        rows=tuple(rounded_row(key, values) for key, values in sorted(totals.items())),
        account_totals=tuple(
            AccountTotal(
                account=account, partition_class=node_class, khours_billed=round(khours, 3)
            )
            for (account, node_class), khours in sorted(account_khours.items())
        ),
    )


def energy_inputs(contributions: list[Contribution]) -> list[EnergyInput]:
    return [
        EnergyInput(
            month=row.month if row.month[:4].isdigit() else None,
            partition_class=row.partition_class,
            billed_node_hours=row.billed_node_hours,
            measured_billed_node_hours=row.measured_billed_node_hours,
            measured_kwh=row.energy_kwh if row.measured_billed_node_hours else 0.0,
            group=contribution.contributor,
        )
        for contribution in contributions
        for row in contribution.rows
    ]


def default_export_path(contribution: Contribution) -> Path:
    return Path("contributions") / contribution.project / f"{contribution.contributor}.json"


def write_contribution(contribution: Contribution, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contribution.to_json(), indent=2) + "\n")

    return path


def parse_contribution(payload: dict[str, Any]) -> Contribution:
    if payload.get("schema") != CONTRIBUTION_SCHEMA:
        raise ContributionError(
            f"not a {CONTRIBUTION_SCHEMA} file (schema={payload.get('schema')!r})"
        )

    try:
        return Contribution(
            contributor=payload["contributor"],
            project=payload["project"],
            generated_at=payload["generated_at"],
            rules_fingerprint=payload["rules_fingerprint"],
            first_job=payload.get("first_job"),
            last_job=payload.get("last_job"),
            last_sync=payload.get("last_sync"),
            rows=tuple(ContributionRow(**row) for row in payload["rows"]),
            account_totals=tuple(AccountTotal(**total) for total in payload["account_totals"]),
        )
    except (KeyError, TypeError) as error:
        raise ContributionError(f"malformed contribution: {error}") from error


def read_contribution(path: Path) -> Contribution:
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise ContributionError(f"{path}: invalid JSON ({error})") from error

    return parse_contribution(payload)


def load_contributions(store: Store, project: str, own_user: str) -> list[Contribution]:
    """Imported contributions for a project, excluding the local user's own (already local)."""
    contributions = [
        parse_contribution(payload) for payload in store.contribution_payloads(project)
    ]

    return [contribution for contribution in contributions if contribution.contributor != own_user]


def reconciliation_warnings(config: Config, store: Store, contribution: Contribution) -> list[str]:
    """Compare the contributor's sacct totals with their own rows in our bsc_acct snapshots."""
    usage_by_account = store.last_user_usage_per_account()
    warnings = []

    for total in contribution.account_totals:
        node_type = config.node_types.get(total.partition_class)
        rows = usage_by_account.get(total.account, [])
        match = next(
            (
                row
                for row in rows
                if node_type
                and row.user == contribution.contributor
                and row.machine == node_type.bsc_acct_machine
            ),
            None,
        )

        if match is None or max(match.used_khours, total.khours_billed) < RECONCILIATION_MIN_KHOURS:
            continue

        diff_pct = (total.khours_billed - match.used_khours) / max(match.used_khours, 1e-9) * 100

        if abs(diff_pct) > RECONCILIATION_WARN_PCT:
            warnings.append(
                f"{contribution.contributor} {total.account} {total.partition_class}: contribution "
                f"{total.khours_billed} vs bsc_acct {match.used_khours} khours "
                f"({diff_pct:+.0f} %); the file may be older than our snapshot, or incomplete"
            )

    return warnings


def import_contribution(config: Config, store: Store, path: Path) -> ImportOutcome:
    try:
        contribution = read_contribution(path)
    except ContributionError as error:
        return ImportOutcome(str(path), None, None, "invalid", [str(error)])

    outcome = ImportOutcome(str(path), contribution.contributor, contribution.project, "", [])

    if contribution.contributor == config.user:
        outcome.status = "skipped: own contribution (your jobs are already in the local store)"
        return outcome

    if not store.save_contribution_payload(
        contribution.to_json(), source=str(path), imported_at=utc_now()
    ):
        outcome.status = "skipped: not newer than the stored contribution"
        return outcome

    outcome.status = "imported"

    if contribution.rules_fingerprint != config.rules_fingerprint:
        outcome.warnings.append(
            f"attributed with different rules ({contribution.rules_fingerprint} vs "
            f"{config.rules_fingerprint}); ask {contribution.contributor} to pull the latest "
            "config/project.yaml and re-export"
        )

    outcome.warnings.extend(reconciliation_warnings(config, store, contribution))

    return outcome
