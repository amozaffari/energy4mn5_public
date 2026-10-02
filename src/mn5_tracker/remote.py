"""Run read-only commands on MN5 over ssh with host fallback and login-noise-proof parsing."""

import re
import shlex
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from mn5_tracker.config import Hosts


SENTINEL_PREFIX = "__MN5TRACK"
SSH_CONNECTION_FAILURE_EXIT_CODE = 255

READ_ONLY_PROGRAMS = frozenset(
    {
        "sacct",
        "sacctmgr",
        "sreport",
        "sshare",
        "bsc_acct",
        "/apps/modules/bsc/bin/bsc_acct",
        "bsc_quota",
        "id",
        "hostname",
    }
)
SACCTMGR_READ_VERBS = frozenset({"show", "list"})
SHELL_METACHARACTERS = re.compile(r"[;&|<>`$\n\\]")

SubprocessRunner = Callable[..., subprocess.CompletedProcess[bytes]]


class RemoteError(RuntimeError):
    """Raised when no MN5 login node could run the requested commands."""


class ForbiddenCommandError(ValueError):
    """Raised when a command is not on the read-only allowlist."""


@dataclass(frozen=True)
class CommandOutput:
    name: str
    command: str
    stdout: str
    exit_code: int | None


@dataclass
class BatchResult:
    host: str
    outputs: dict[str, CommandOutput]
    stderr: str
    duration_s: float
    failed_attempts: list[str] = field(default_factory=list)


def assert_read_only(command: str) -> None:
    """Refuse anything that could modify state on MN5; the tracker must stay read-only."""
    if SHELL_METACHARACTERS.search(command):
        raise ForbiddenCommandError(f"Shell metacharacters are not allowed: {command!r}")

    tokens = shlex.split(command)

    if not tokens or tokens[0] not in READ_ONLY_PROGRAMS:
        raise ForbiddenCommandError(f"Program not on the read-only allowlist: {command!r}")

    if tokens[0] != "sacctmgr":
        return

    verbs = [token for token in tokens[1:] if not token.startswith("-")]

    if not verbs or verbs[0] not in SACCTMGR_READ_VERBS:
        raise ForbiddenCommandError(f"sacctmgr is only allowed with show/list: {command!r}")


def begin_marker(name: str) -> str:
    return f"{SENTINEL_PREFIX}_BEGIN_{name}__"


def end_marker(name: str) -> str:
    return f"{SENTINEL_PREFIX}_END_{name}__"


def build_remote_script(commands: Mapping[str, str]) -> str:
    """Wrap every command in sentinels so module-load noise from the login shell is ignored."""
    parts = []

    for name, command in commands.items():
        assert_read_only(command)
        parts.append(f"echo {begin_marker(name)}; {command}; echo {end_marker(name)} $?")

    return "; ".join(parts)


def extract_sections(stdout: str, commands: Mapping[str, str]) -> dict[str, CommandOutput]:
    """Return the text between each command's sentinels, plus its remote exit code."""
    lines = stdout.splitlines()
    outputs = {}

    for name, command in commands.items():
        begin = begin_marker(name)
        end = end_marker(name)
        start_index = next((i for i, line in enumerate(lines) if line.strip() == begin), None)

        if start_index is None:
            continue

        body = []
        exit_code = None

        for line in lines[start_index + 1 :]:
            if line.startswith(end):
                exit_code = parse_exit_code(line, end)
                break

            body.append(line)

        outputs[name] = CommandOutput(
            name=name,
            command=command,
            stdout="\n".join(body) + ("\n" if body else ""),
            exit_code=exit_code,
        )

    return outputs


def parse_exit_code(end_line: str, end: str) -> int | None:
    remainder = end_line[len(end) :].strip()

    return int(remainder) if remainder.isdigit() else None


class RemoteRunner:
    """Runs a batch of read-only commands in a single ssh session, falling back across hosts."""

    hosts: Hosts
    run_subprocess: SubprocessRunner

    def __init__(self, hosts: Hosts, run_subprocess: SubprocessRunner = subprocess.run) -> None:
        self.hosts = hosts
        self.run_subprocess = run_subprocess

    def ssh_command(self, host: str, script: str) -> list[str]:
        return [
            "ssh",
            "-o",
            f"ConnectTimeout={self.hosts.connect_timeout_s}",
            "-o",
            "BatchMode=yes",
            host,
            script,
        ]

    def run_batch(self, commands: Mapping[str, str]) -> BatchResult:
        script = build_remote_script(commands)
        failed_attempts = []

        for host in (self.hosts.primary, self.hosts.fallback):
            started = time.monotonic()

            try:
                completed = self.run_subprocess(
                    self.ssh_command(host, script),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=self.hosts.command_timeout_s,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                failed_attempts.append(f"{host}: timed out after {self.hosts.command_timeout_s} s")
                continue
            except OSError as error:
                failed_attempts.append(f"{host}: could not start ssh ({error})")
                continue

            stdout = completed.stdout.decode("utf-8", errors="replace")
            stderr = completed.stderr.decode("utf-8", errors="replace")

            if completed.returncode == SSH_CONNECTION_FAILURE_EXIT_CODE:
                failed_attempts.append(f"{host}: ssh connection failed: {last_line(stderr)}")
                continue

            outputs = extract_sections(stdout, commands)

            if not outputs:
                failed_attempts.append(f"{host}: no sentinel output (exit {completed.returncode})")
                continue

            return BatchResult(
                host=host,
                outputs=outputs,
                stderr=stderr,
                duration_s=time.monotonic() - started,
                failed_attempts=failed_attempts,
            )

        raise RemoteError("All MN5 hosts failed: " + "; ".join(failed_attempts))


def last_line(text: str) -> str:
    stripped = text.strip().splitlines()

    return stripped[-1] if stripped else ""
