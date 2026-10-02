from dataclasses import replace
from pathlib import Path

import pytest

from mn5_tracker.config import Config, load_config
from mn5_tracker.remote import CommandOutput
from mn5_tracker.store import Store


FIXTURES = Path(__file__).parent / "fixtures"
CONFIG_DIR = Path(__file__).parents[1] / "config"
EXAMPLE_CONFIG = CONFIG_DIR / "tracker.example.yaml"
EXAMPLE_PROJECT_CONFIG = CONFIG_DIR / "project.example.yaml"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def between_sentinels(text: str) -> str:
    """Fixtures were captured with plain __BEGIN__/__END__ markers around the command."""
    return text.split("__BEGIN__\n", 1)[1].rsplit("__END__", 1)[0]


FIXTURE_USER = "bsc000000"


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """The personal example merged with project.example.yaml, as the fixtures' user.

    The example project file is named explicitly so a member's real config/project.yaml never
    leaks into the tests.
    """
    personal = tmp_path / "tracker.yaml"
    personal.write_text(
        EXAMPLE_CONFIG.read_text() + f"\nproject_config: {EXAMPLE_PROJECT_CONFIG}\n"
    )

    return replace(load_config(personal), user=FIXTURE_USER)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "tracker.db")


def command_outputs(
    sacct: str | None = None, bsc_acct: str | None = None, groups: str | None = None
) -> dict[str, CommandOutput]:
    texts = {
        "sacct": sacct if sacct is not None else fixture_text("sacct.psv"),
        "bsc_acct": (
            bsc_acct if bsc_acct is not None else between_sentinels(fixture_text("bsc_acct.txt"))
        ),
        "associations": between_sentinels(fixture_text("sacctmgr_assoc.txt")),
        "groups": groups or "bsc earth acc_mn5 bsc32 ehpc101 ehpc102 ehpc103 ehpc104\n",
        "quota": between_sentinels(fixture_text("bsc_quota.txt")),
    }

    return {
        name: CommandOutput(name=name, command=name, stdout=text, exit_code=0)
        for name, text in texts.items()
    }
