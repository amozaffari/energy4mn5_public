import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from conftest import command_outputs

from mn5_tracker.aggregation import AllocationUsage, load_attributed_jobs, project_lifetime
from mn5_tracker.config import Config
from mn5_tracker.contributions import (
    AccountTotal,
    Contribution,
    ContributionRow,
    build_contribution,
    import_contribution,
    write_contribution,
)
from mn5_tracker.metrics import khours_to_node_hours
from mn5_tracker.reports.markdown import build_statement
from mn5_tracker.store import Store
from mn5_tracker.sync import ingest_outputs


SNAPSHOT = "2026-09-29T09:00:00+00:00"
COLLEAGUE = "bsc900011"
COLLEAGUE_EHPC103_KHOURS = 67.25


def colleague_contribution(
    config: Config,
    rows: list[tuple[str, str, float]],
    generated_at: str = "2026-09-29T10:00:00+00:00",
    contributor: str = COLLEAGUE,
) -> Contribution:
    """A contribution with one COMPLETED ACC row per (month, account, billed node-h)."""
    return Contribution(
        contributor=contributor,
        project="geofm",
        generated_at=generated_at,
        rules_fingerprint=config.rules_fingerprint,
        first_job="2025-10-01T00:00:00",
        last_job="2026-09-01T00:00:00",
        last_sync=generated_at,
        rows=tuple(
            ContributionRow(
                month=month,
                account=account,
                partition_class="acc",
                state="COMPLETED",
                jobs=10,
                node_hours=hours,
                billed_node_hours=hours,
                gpu_hours_billed=hours * 4,
                gpu_hours_requested=hours * 4,
                core_hours_billed=hours * 80,
                energy_kwh=0.0,
            )
            for month, account, hours in rows
        ),
        account_totals=tuple(
            AccountTotal(account=account, partition_class="acc", khours_billed=hours * 80 / 1000)
            for _, account, hours in rows
        ),
    )


def import_file(config: Config, store: Store, tmp_path: Path, contribution: Contribution) -> str:
    path = write_contribution(contribution, tmp_path / f"{contribution.contributor}.json")

    return import_contribution(config, store, path).status


def ehpc103(lifetime_allocations: list[AllocationUsage]) -> AllocationUsage:
    return next(
        allocation for allocation in lifetime_allocations if allocation.account == "ehpc103"
    )


@pytest.fixture
def synced(config: Config, store: Store) -> Store:
    ingest_outputs(config, store, SNAPSHOT, command_outputs())

    return store


def test_export_contains_only_aggregates(config: Config, synced: Store) -> None:
    contribution = build_contribution(config, synced, load_attributed_jobs(config, synced), "geofm")
    text = json.dumps(contribution.to_json())

    assert contribution.contributor == config.user
    assert contribution.rows
    assert "/gpfs" not in text
    assert "repro_ar_unet" not in text
    assert "46783172" not in text


def test_contribution_replaces_bsc_acct_fallback_without_double_counting(
    config: Config, synced: Store, tmp_path: Path
) -> None:
    jobs = load_attributed_jobs(config, synced)
    before = ehpc103(project_lifetime(config, synced, jobs, "geofm").allocations)
    colleague_node_hours = khours_to_node_hours(COLLEAGUE_EHPC103_KHOURS, config.node_types["acc"])

    # Same amount as bsc_acct reports for that colleague: the total must not change.
    contribution = colleague_contribution(config, [("2026-08", "ehpc103", colleague_node_hours)])

    assert import_file(config, synced, tmp_path, contribution) == "imported"

    after = ehpc103(project_lifetime(config, synced, jobs, "geofm").allocations)

    assert after.contributors == [COLLEAGUE]
    assert after.contributed_billed_node_hours_by_class["acc"] == pytest.approx(
        colleague_node_hours
    )
    fallback_before = before.bsc_acct_others_billed_node_hours_by_class or {}
    fallback_after = after.bsc_acct_others_billed_node_hours_by_class or {}

    assert fallback_after["acc"] == pytest.approx(fallback_before["acc"] - colleague_node_hours)
    assert after.total_billed_node_hours_by_class["acc"] == pytest.approx(
        before.total_billed_node_hours_by_class["acc"]
    )


def test_contribution_uses_project_share_not_bsc_acct_total(
    config: Config, synced: Store, tmp_path: Path
) -> None:
    """A member who spent part of the allocation on other work counts only their project jobs."""
    jobs = load_attributed_jobs(config, synced)
    before = project_lifetime(config, synced, jobs, "geofm")
    colleague_node_hours = khours_to_node_hours(COLLEAGUE_EHPC103_KHOURS, config.node_types["acc"])
    import_file(
        config, synced, tmp_path, colleague_contribution(config, [("2026-08", "ehpc103", 100.0)])
    )
    after = project_lifetime(config, synced, jobs, "geofm")

    assert after.lower_bound_billed_node_hours_by_class["acc"] == pytest.approx(
        before.lower_bound_billed_node_hours_by_class["acc"] - colleague_node_hours + 100.0
    )


def test_contribution_fills_lapsed_allocation(
    config: Config, synced: Store, tmp_path: Path
) -> None:
    jobs = load_attributed_jobs(config, synced)
    before = project_lifetime(config, synced, jobs, "geofm")
    import_file(
        config, synced, tmp_path, colleague_contribution(config, [("2025-12", "ehpc106", 500.0)])
    )
    after = project_lifetime(config, synced, jobs, "geofm")
    ehpc106 = next(
        allocation for allocation in after.allocations if allocation.account == "ehpc106"
    )

    assert ehpc106.contributors == [COLLEAGUE]
    assert after.lower_bound_billed_node_hours_by_class["acc"] == pytest.approx(
        before.lower_bound_billed_node_hours_by_class["acc"] + 500.0
    )
    assert after.monthly_billed_node_hours["2025-12"]["acc"] >= 500.0
    assert after.members_with_detail == 2


def test_import_skips_own_older_and_invalid(config: Config, synced: Store, tmp_path: Path) -> None:
    newer = colleague_contribution(config, [("2026-08", "ehpc103", 1.0)])
    older = replace(newer, generated_at="2026-01-01T00:00:00+00:00")
    own = replace(newer, contributor=config.user)
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({"schema": "something-else"}))

    assert import_file(config, synced, tmp_path, newer) == "imported"
    assert import_file(config, synced, tmp_path, older).startswith("skipped: not newer")
    assert import_file(config, synced, tmp_path, own).startswith("skipped: own")
    assert import_contribution(config, synced, invalid).status == "invalid"


def test_import_warns_on_different_rules_and_mismatched_totals(
    config: Config, synced: Store, tmp_path: Path
) -> None:
    contribution = replace(
        colleague_contribution(config, [("2026-08", "ehpc103", 100.0)]), rules_fingerprint="0000"
    )
    path = write_contribution(contribution, tmp_path / "colleague.json")
    warnings = import_contribution(config, synced, path).warnings

    assert any("different rules" in warning for warning in warnings)
    assert any("vs bsc_acct 67.25 khours" in warning for warning in warnings)


def test_statement_reports_coverage_without_names(
    config: Config, synced: Store, tmp_path: Path
) -> None:
    import_file(
        config, synced, tmp_path, colleague_contribution(config, [("2026-08", "ehpc103", 10.0)])
    )
    lifetime = project_lifetime(config, synced, load_attributed_jobs(config, synced), "geofm")
    text = build_statement(config, lifetime, date(2026, 9, 29))

    assert "Slurm accounting of 2 project members" in text
    assert COLLEAGUE not in text
    assert config.user not in text
