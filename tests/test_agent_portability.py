"""Skills load in any Agent Skills runtime (Hermes Agent, Claude Code), and the Hermes cron
script follows Hermes' delivery contract."""

import re
from pathlib import Path

import pytest
import yaml

from mn5_tracker import scheduling
from mn5_tracker.alerts import Alert
from mn5_tracker.cli import alert_message, app


REPO = Path(__file__).parents[1]
SKILLS_DIR = REPO / ".agents" / "skills"
SKILL_PATHS = sorted(SKILLS_DIR.glob("*/SKILL.md"))
REQUIRED_SECTIONS = ("When to Use", "Quick Reference", "Procedure", "Pitfalls", "Verification")
SLUG = re.compile(r"^[a-z][a-z0-9_-]*$")


def frontmatter_and_body(path: Path) -> tuple[dict, str]:
    _, frontmatter, body = path.read_text(encoding="utf-8").split("---", 2)

    return yaml.safe_load(frontmatter), body


def test_every_skill_is_found() -> None:
    assert len(SKILL_PATHS) == 6


@pytest.mark.parametrize("path", SKILL_PATHS, ids=lambda path: path.parent.name)
def test_skill_frontmatter_follows_agent_skills_format(path: Path) -> None:
    meta, _ = frontmatter_and_body(path)

    assert SLUG.match(path.parent.name)
    assert meta["name"] == path.parent.name
    assert 20 <= len(meta["description"]) <= 1024
    assert meta["version"]
    assert set(meta["platforms"]) <= {"macos", "linux", "windows"}


@pytest.mark.parametrize("path", SKILL_PATHS, ids=lambda path: path.parent.name)
def test_skill_body_has_hermes_sections_and_no_harness_placeholders(path: Path) -> None:
    _, body = frontmatter_and_body(path)
    headings = re.findall(r"^## (.+)$", body, re.MULTILINE)

    assert list(REQUIRED_SECTIONS) == [
        heading for heading in headings if heading in REQUIRED_SECTIONS
    ]
    assert "$ARGUMENTS" not in body


@pytest.mark.parametrize("path", SKILL_PATHS, ids=lambda path: path.parent.name)
def test_skills_only_name_commands_that_exist(path: Path) -> None:
    commands = {
        command.name or getattr(command.callback, "__name__", "").replace("_", "-")
        for command in app.registered_commands
    }
    named = set(re.findall(r"uv run mn5track ([a-z][a-z-]*)", path.read_text(encoding="utf-8")))

    assert named

    assert named <= commands


def test_claude_code_reads_the_same_skills() -> None:
    claude_skills = REPO / ".claude" / "skills"

    assert claude_skills.is_symlink()
    assert claude_skills.resolve() == SKILLS_DIR.resolve()


def test_alert_message_is_silent_without_alerts() -> None:
    assert alert_message([]) == ""


def test_alert_message_lists_critical_first() -> None:
    found = [
        Alert("warning", "budget", "ehpc101", "ehpc101 at 85% used"),
        Alert("critical", "expiry", "ehpc104", "ehpc104 expires today"),
    ]
    lines = alert_message(found).splitlines()

    assert lines[0] == "MN5 compute tracker: 2 alerts"
    assert lines[1].startswith("- [critical]")


def test_hermes_script_is_quiet_on_success_and_fails_only_on_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = scheduling.install_hermes_script()
    script = path.read_text()

    assert path == tmp_path / "scripts" / scheduling.HERMES_SCRIPT_NAME
    assert path.stat().st_mode & 0o111
    assert "mn5track alerts --message" in script.splitlines()[-1]
    assert "exit 1" in script
    assert "--script mn5track-daily.sh --deliver telegram" in scheduling.hermes_cron_command(
        7, "telegram"
    )


def test_hermes_script_carries_config_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MN5TRACK_CONFIG", "/somewhere/tracker.yaml")

    assert "export MN5TRACK_CONFIG=/somewhere/tracker.yaml" in scheduling.hermes_script()
