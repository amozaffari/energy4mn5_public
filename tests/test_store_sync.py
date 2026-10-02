import re
from dataclasses import replace
from datetime import date
from pathlib import Path

from conftest import between_sentinels, command_outputs, fixture_text

from mn5_tracker.aggregation import aggregate, load_attributed_jobs, project_lifetime, total
from mn5_tracker.alerts import evaluate_alerts
from mn5_tracker.allocation_status import compute_statuses
from mn5_tracker.config import Config
from mn5_tracker.reports.markdown import build_statement
from mn5_tracker.store import JobRecord, Store
from mn5_tracker.sync import determine_since, ingest_outputs, run_reparse, save_raw_outputs


FIRST_SNAPSHOT = "2026-09-28T09:00:00+00:00"
SECOND_SNAPSHOT = "2026-09-29T09:00:00+00:00"
TODAY = date(2026, 9, 29)


def stored_job(store: Store, job_id: str) -> JobRecord:
    job = store.get_job(job_id)
    assert job is not None

    return job


def remove_account_section(bsc_acct_text: str, account: str) -> str:
    """Simulate lost membership: drop one group's block from bsc_acct output."""
    blocks = bsc_acct_text.split("\n\n\n")

    return "\n\n\n".join(block for block in blocks if f"({account})" not in block)


def test_ingest_stores_jobs_snapshots_and_visibility(config: Config, store: Store) -> None:
    result = ingest_outputs(config, store, FIRST_SNAPSHOT, command_outputs())

    assert result.errors == []
    assert result.jobs_new == 13
    assert result.budget_rows > 0 and result.user_rows > 0
    assert result.association_rows == len(
        set(config.allocations) | {"ehpc101", "ehpc102", "ehpc103", "ehpc104", "bsc32"}
    )
    assert result.quota_rows == 15


def test_terminal_jobs_are_immutable(config: Config, store: Store) -> None:
    ingest_outputs(config, store, FIRST_SNAPSHOT, command_outputs())
    changed = fixture_text("sacct.psv").replace("|COMPLETED|2025-02-27", "|FAILED|2025-02-27")
    result = ingest_outputs(config, store, SECOND_SNAPSHOT, command_outputs(sacct=changed))

    assert result.jobs_new == 0
    assert stored_job(store, "16113552").state == "COMPLETED"


def test_running_job_is_updated(config: Config, store: Store) -> None:
    ingest_outputs(config, store, FIRST_SNAPSHOT, command_outputs())
    finished = fixture_text("sacct.psv").replace(
        "|RUNNING|2026-09-29T08:40:01|2026-09-29T08:48:23|Unknown|11632|",
        "|COMPLETED|2026-09-29T08:40:01|2026-09-29T08:48:23|2026-09-29T12:00:00|11857|",
    )
    ingest_outputs(config, store, SECOND_SNAPSHOT, command_outputs(sacct=finished))
    job = stored_job(store, "46783172")

    assert job.state == "COMPLETED"
    assert job.first_seen == FIRST_SNAPSHOT
    assert job.last_updated == SECOND_SNAPSHOT


def test_lost_visibility_raises_alert_and_keeps_final_snapshot(
    config: Config, store: Store
) -> None:
    ingest_outputs(config, store, FIRST_SNAPSHOT, command_outputs())
    bsc_acct = remove_account_section(between_sentinels(fixture_text("bsc_acct.txt")), "ehpc101")
    groups = "bsc earth acc_mn5 bsc32 ehpc102 ehpc103 ehpc104\n"
    result = ingest_outputs(
        config, store, SECOND_SNAPSHOT, command_outputs(bsc_acct=bsc_acct, groups=groups)
    )

    lost = {(change.account, change.source) for change in result.visibility_changes}

    assert ("ehpc101", "in_bsc_acct") in lost
    assert ("ehpc101", "in_unix_group") in lost

    statuses = compute_statuses(config, store, TODAY)
    ehpc101 = next(status for status in statuses if status.account == "ehpc101")

    assert not ehpc101.visible_in_bsc_acct
    assert ehpc101.snapshot_ts == FIRST_SNAPSHOT
    assert ehpc101.used_khours == 221.68

    alerts = evaluate_alerts(config, store, statuses, TODAY)

    assert any(alert.kind == "visibility" and alert.account == "ehpc101" for alert in alerts)


def test_expiry_and_stale_alerts(config: Config, store: Store) -> None:
    assert [alert.kind for alert in evaluate_alerts(config, store, [], TODAY)] == ["stale"]

    ingest_outputs(config, store, FIRST_SNAPSHOT, command_outputs())
    alerts = evaluate_alerts(config, store, compute_statuses(config, store, TODAY), TODAY)
    expiry = [alert for alert in alerts if alert.kind == "expiry"]

    assert [alert.account for alert in expiry] == ["ehpc104"]
    assert expiry[0].severity == "critical"


def test_budget_threshold_alert(config: Config, store: Store) -> None:
    ingest_outputs(config, store, FIRST_SNAPSHOT, command_outputs())
    strict = replace(config, alerts=replace(config.alerts, used_pct_warn=75))
    alerts = evaluate_alerts(strict, store, compute_statuses(strict, store, TODAY), TODAY)

    assert any(alert.kind == "budget" and alert.account == "ehpc101" for alert in alerts)


def test_report_aggregation_separates_waste_and_timeout(config: Config, store: Store) -> None:
    ingest_outputs(config, store, FIRST_SNAPSHOT, command_outputs())
    jobs = load_attributed_jobs(config, store)
    summary = total(jobs)
    by_state = {row.key: row for row in aggregate(jobs, "state")}

    assert summary.jobs == 13
    assert summary.active_jobs == 3
    assert summary.timeout_jobs == 1
    assert by_state["TIMEOUT"].timeout_billed_node_hours > 0

    # Fixture: 2 CANCELLED in gp_bsces (production); FAILED and OUT_OF_MEMORY in debug QOS.
    failures = by_state["FAILED"].jobs + by_state["CANCELLED"].jobs + by_state["OUT_OF_MEMORY"].jobs

    assert summary.wasted_jobs == 2
    assert summary.debug_failed_jobs == 2
    assert summary.wasted_jobs + summary.debug_failed_jobs == failures


def test_production_success_rate_ignores_debug_queues(config: Config, store: Store) -> None:
    ingest_outputs(config, store, FIRST_SNAPSHOT, command_outputs())
    jobs = load_attributed_jobs(config, store)
    by_queue = {row.key: row for row in aggregate(jobs, "queue")}
    summary = total(jobs)

    assert set(by_queue) == {"development", "production"}
    assert by_queue["development"].jobs == 10
    assert summary.production_success_rate == 0.0
    assert summary.success_rate == 5 / 10

    everything_production = replace(config, development_qos=())
    assert total(load_attributed_jobs(everything_production, store)).debug_failed_jobs == 0


def test_statement_never_names_colleagues(config: Config, store: Store) -> None:
    ingest_outputs(config, store, FIRST_SNAPSHOT, command_outputs())
    lifetime = project_lifetime(config, store, load_attributed_jobs(config, store), "geofm")
    text = build_statement(config, lifetime, TODAY)

    assert "Colleague" not in text
    assert not re.search(r"bsc9\d{5}", text)
    assert "other project members" in text
    assert lifetime.others_billed_node_hours_by_class["acc"] > 0


def test_project_lifetime_lists_invisible_allocations(config: Config, store: Store) -> None:
    ingest_outputs(config, store, FIRST_SNAPSHOT, command_outputs())
    lifetime = project_lifetime(config, store, load_attributed_jobs(config, store), "geofm")
    invisible = {item["account"] for item in lifetime.invisible}

    assert {"ehpc105", "ehpc106", "ehpc107"} <= invisible
    assert "ehpc103" not in invisible


def test_incremental_window(config: Config, store: Store) -> None:
    assert determine_since(store, config, None, full=False) == config.first_job_date

    ingest_outputs(config, store, FIRST_SNAPSHOT, command_outputs())

    assert determine_since(store, config, None, full=False) == date(2026, 9, 22)
    assert determine_since(store, config, None, full=True) == config.first_job_date


def test_reparse_rebuilds_from_raw(config: Config, tmp_path: Path) -> None:
    store = Store(tmp_path / "db" / "tracker.db")
    outputs = command_outputs()
    save_raw_outputs(store.raw_dir, FIRST_SNAPSHOT, "MN5G1", date(2024, 4, 1), outputs)
    ingest_outputs(config, store, FIRST_SNAPSHOT, outputs)
    before = len(store.load_jobs())

    results = run_reparse(config, store)

    assert len(results) == 1 and results[0].errors == []
    assert len(store.load_jobs()) == before
    assert store.snapshot_timestamps("budget_snapshots") == [FIRST_SNAPSHOT]
