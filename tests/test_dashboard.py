import json
import re
from datetime import date
from pathlib import Path

import pytest
from conftest import command_outputs

from mn5_tracker import scheduling
from mn5_tracker.config import Config, ConfigError, parse_dashboards
from mn5_tracker.reports.dashboard import build_dashboard_data, month_range, render_dashboard
from mn5_tracker.store import Store
from mn5_tracker.sync import ingest_outputs


TODAY = date(2026, 9, 29)


def embedded_data(html: str) -> dict:
    match = re.search(
        r'<script type="application/json" id="dashboard-data">(.*?)</script>', html, re.S
    )
    assert match

    return json.loads(match.group(1))


def test_month_range_fills_gaps() -> None:
    assert month_range(["2025-11", "2026-02"]) == ["2025-11", "2025-12", "2026-01", "2026-02"]
    assert month_range([]) == []


def test_own_dashboard_matches_report_totals(config: Config, store: Store) -> None:
    ingest_outputs(config, store, "2026-09-29T09:00:00+00:00", command_outputs())
    data = build_dashboard_data(config, store, TODAY)
    monthly_total = sum(sum(month["values"].values()) for month in data["monthly"]["project"])
    outcome_total = sum(
        bucket["billed_node_hours"] for bucket in data["outcomes"]["totals"].values()
    )

    assert data["scope"]["kind"] == "own"
    assert data["group_keys"] == ["project", "account", "queue"]
    assert {month["month"] for month in data["monthly"]["queue"]}
    assert {row["name"] for row in data["carbon"]["by_queue"]} <= {"production", "development"}
    assert monthly_total == outcome_total or abs(monthly_total - outcome_total) < 0.1
    assert data["tiles"]["jobs"] == 13
    assert data["tiles"]["active_allocations"] == 4
    assert data["tiles"]["debug_failed_jobs"] == 2
    assert data["tiles"]["production_success_rate"] == 0.0
    assert set(data["outcomes"]["totals"]) == {
        "completed",
        "timeout",
        "wasted",
        "debug_failed",
        "active",
    }
    assert {panel["account"] for panel in data["allocations"]} <= set(data["series"]["account"])
    assert any(alert["kind"] == "expiry" for alert in data["health"]["alerts"])


def test_project_dashboard_uses_lifetime_and_hides_names(config: Config, store: Store) -> None:
    ingest_outputs(config, store, "2026-09-29T09:00:00+00:00", command_outputs())
    data = build_dashboard_data(config, store, TODAY, project="geofm")
    html = render_dashboard(data)

    assert data["scope"]["coverage"]["members_with_detail"] == 1
    assert data["tiles"]["acc_node_hours"] > 0
    assert "Colleague" not in html
    assert embedded_data(html)["schema"] == "mn5track.dashboard/v1"


def test_embedded_json_cannot_close_the_script_tag(config: Config, store: Store) -> None:
    data = build_dashboard_data(config, store, TODAY)
    data["notes"].append("</script><script>alert(1)</script>")
    html = render_dashboard(data)

    assert "</script><script>alert(1)" not in html
    assert embedded_data(html)["notes"][-1] == "</script><script>alert(1)</script>"


def test_dashboard_settings_expand_home_and_reject_unknown_projects(config: Config) -> None:
    settings = parse_dashboards({"out_dir": "~/Drive", "projects": ["geofm"]}, config.projects)

    assert settings.out_dir == Path.home() / "Drive"
    assert settings.projects == ("geofm",)

    with pytest.raises(ConfigError, match="unknown projects"):
        parse_dashboards({"projects": ["nope"]}, config.projects)


def test_schedule_rebuilds_every_dashboard() -> None:
    assert "mn5track dashboard --all;" in scheduling.sync_script()
