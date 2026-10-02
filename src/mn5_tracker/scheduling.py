"""Daily `mn5track sync` on the user's Mac via a launchd agent (never cron on MN5)."""

import os
import plistlib
import shlex
import shutil
import subprocess
from pathlib import Path

from mn5_tracker.config import REPO_ROOT


LABEL = "com.mn5track.sync"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_PATH = Path.home() / "Library" / "Logs" / "mn5track.log"


def sync_script() -> str:
    """Sync, refresh the dashboards, then alerts; a notification fires when alerts are raised."""
    uv = shlex.quote(shutil.which("uv") or "uv")
    project = shlex.quote(str(REPO_ROOT))
    run = f"{uv} run --project {project} mn5track"
    notify = (
        'osascript -e \'display notification "Run mn5track alerts for details" '
        'with title "MN5 compute tracker"\''
    )

    return f"date; {run} sync; {run} dashboard --all; {run} alerts || {notify}"


def render_plist(hour: int) -> str:
    plist = {
        "Label": LABEL,
        "ProgramArguments": ["/bin/zsh", "-lc", sync_script()],
        "StartCalendarInterval": {"Hour": hour, "Minute": 0},
        # launchd skips runs while the Mac sleeps; RunAtLoad catches up after a reboot or login.
        "RunAtLoad": True,
        "StandardOutPath": str(LOG_PATH),
        "StandardErrorPath": str(LOG_PATH),
        "WorkingDirectory": str(REPO_ROOT),
    }

    return plistlib.dumps(plist).decode()


def launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def install(hour: int) -> Path:
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["launchctl", "bootout", launchd_domain(), str(PLIST_PATH)], check=False)
    PLIST_PATH.write_text(render_plist(hour))
    subprocess.run(["launchctl", "bootstrap", launchd_domain(), str(PLIST_PATH)], check=True)

    return PLIST_PATH


def uninstall() -> None:
    subprocess.run(["launchctl", "bootout", launchd_domain(), str(PLIST_PATH)], check=False)
    PLIST_PATH.unlink(missing_ok=True)
