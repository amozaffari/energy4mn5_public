import subprocess
from typing import Any

import pytest
from conftest import fixture_text

from mn5_tracker.config import Hosts
from mn5_tracker.remote import (
    ForbiddenCommandError,
    RemoteError,
    RemoteRunner,
    assert_read_only,
    begin_marker,
    end_marker,
    extract_sections,
)


HOSTS = Hosts(primary="MN5G1", fallback="MN5ACC1", connect_timeout_s=15, command_timeout_s=60)
COMMANDS = {"groups": "id -Gn"}


def sentinel_output(body: str, exit_code: int = 0) -> bytes:
    noise = fixture_text("login_noise.txt")

    return f"{noise}{begin_marker('groups')}\n{body}{end_marker('groups')} {exit_code}\n".encode()


class FakeSsh:
    """Records calls and replays one scripted outcome per host."""

    outcomes: dict[str, Any]
    calls: list[list[str]]

    def __init__(self, outcomes: dict[str, Any]) -> None:
        self.outcomes = outcomes
        self.calls = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(args)
        outcome = self.outcomes[args[5]]

        if isinstance(outcome, BaseException):
            raise outcome

        return outcome


def completed(
    returncode: int, stdout: bytes = b"", stderr: bytes = b""
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_sections_ignore_login_noise() -> None:
    outputs = extract_sections(sentinel_output("bsc bsc32\n").decode(), COMMANDS)

    assert outputs["groups"].stdout == "bsc bsc32\n"
    assert outputs["groups"].exit_code == 0


def test_primary_success_uses_connect_timeout() -> None:
    fake = FakeSsh({"MN5G1": completed(0, sentinel_output("bsc\n"))})
    result = RemoteRunner(HOSTS, fake).run_batch(COMMANDS)

    assert result.host == "MN5G1"
    assert fake.calls[0][:5] == ["ssh", "-o", "ConnectTimeout=15", "-o", "BatchMode=yes"]


def test_primary_timeout_falls_back() -> None:
    fake = FakeSsh(
        {
            "MN5G1": subprocess.TimeoutExpired(cmd="ssh", timeout=60),
            "MN5ACC1": completed(0, sentinel_output("bsc\n")),
        }
    )
    result = RemoteRunner(HOSTS, fake).run_batch(COMMANDS)

    assert result.host == "MN5ACC1"
    assert "MN5G1: timed out" in result.failed_attempts[0]


def test_connection_refused_falls_back() -> None:
    fake = FakeSsh(
        {
            "MN5G1": completed(255, stderr=b"ssh: connect to host glogin1: Connection refused\n"),
            "MN5ACC1": completed(0, sentinel_output("bsc\n")),
        }
    )

    assert RemoteRunner(HOSTS, fake).run_batch(COMMANDS).host == "MN5ACC1"


def test_both_hosts_failing_raises() -> None:
    fake = FakeSsh({"MN5G1": completed(255), "MN5ACC1": completed(255)})

    with pytest.raises(RemoteError, match="All MN5 hosts failed"):
        RemoteRunner(HOSTS, fake).run_batch(COMMANDS)


def test_remote_command_failure_is_reported_not_retried() -> None:
    fake = FakeSsh({"MN5G1": completed(0, sentinel_output("", exit_code=1))})
    result = RemoteRunner(HOSTS, fake).run_batch(COMMANDS)

    assert result.outputs["groups"].exit_code == 1
    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    "command",
    [
        "sbatch job.sh",
        "scancel 123",
        "sacctmgr modify user x set qos=y",
        "sacct -u x; rm -rf ~",
        "id -Gn && touch x",
        "sacct -u $USER",
    ],
)
def test_write_commands_are_refused(command: str) -> None:
    with pytest.raises(ForbiddenCommandError):
        assert_read_only(command)


def test_read_commands_are_allowed() -> None:
    assert_read_only("sacctmgr -n -P show assoc user=bsc000000 format=Account,QOS")
    assert_read_only("/apps/modules/bsc/bin/bsc_acct")
