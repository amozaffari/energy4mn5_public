"""Collect from MN5, keep gzipped raw outputs, and ingest them into the store (also for reparse)."""

import gzip
import json
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from mn5_tracker.attribution import attribute
from mn5_tracker.collectors import ParseError
from mn5_tracker.collectors.associations import (
    GROUPS_COMMAND,
    build_associations_command,
    parse_associations,
    parse_groups,
)
from mn5_tracker.collectors.bsc_acct import BSC_ACCT_COMMAND, BscAcctReport, parse_bsc_acct
from mn5_tracker.collectors.quota import QUOTA_COMMAND, parse_quota
from mn5_tracker.collectors.sacct import build_sacct_command, parse_sacct
from mn5_tracker.config import Config
from mn5_tracker.metrics import check_node_spec, compute_usage
from mn5_tracker.remote import CommandOutput, RemoteError, RemoteRunner
from mn5_tracker.store import AssociationSnapshotRow, JobRecord, Store, SyncLogEntry, utc_now


RECONCILIATION_WARN_PCT = 10.0
RECONCILIATION_MIN_KHOURS = 1.0
MAX_LISTED_NEW_JOBS = 20


@dataclass(frozen=True)
class VisibilityChange:
    account: str
    source: str
    before: bool
    after: bool


@dataclass(frozen=True)
class Reconciliation:
    """Own usage per account: sacct billing vs bsc_acct's row for this user (both in khours)."""

    account: str
    machine: str
    sacct_khours: float
    bsc_acct_khours: float
    diff_pct: float | None


@dataclass
class IngestResult:
    snapshot_ts: str
    jobs_seen: int = 0
    jobs_new: int = 0
    jobs_updated: int = 0
    new_jobs: list[dict[str, str]] = field(default_factory=list)
    budget_rows: int = 0
    user_rows: int = 0
    association_rows: int = 0
    quota_rows: int = 0
    visibility_changes: list[VisibilityChange] = field(default_factory=list)
    reconciliation: list[Reconciliation] = field(default_factory=list)
    attribution_changes: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class SyncSummary:
    host: str | None
    since: str
    duration_s: float
    failed_attempts: list[str]
    result: IngestResult
    raw_dir: str | None

    def to_json(self) -> dict[str, object]:
        return {"schema": "mn5track.sync/v1", **asdict(self)}


def determine_since(store: Store, config: Config, since: date | None, full: bool) -> date:
    """Incremental window: last max Submit minus the overlap, so state changes are re-read."""
    if since:
        return since

    latest_submit = store.latest_submit()

    if full or not latest_submit:
        return config.first_job_date

    return date.fromisoformat(latest_submit[:10]) - timedelta(days=config.sync.overlap_days)


def build_commands(config: Config, since: date) -> dict[str, str]:
    commands = {
        "sacct": build_sacct_command(config.user, since),
        "bsc_acct": BSC_ACCT_COMMAND,
        "associations": build_associations_command(config.user),
        "groups": GROUPS_COMMAND,
    }

    if config.sync.include_quota:
        commands["quota"] = QUOTA_COMMAND

    return commands


def compact_timestamp(snapshot_ts: str) -> str:
    return datetime.fromisoformat(snapshot_ts).strftime("%Y%m%dT%H%M%SZ")


def save_raw_outputs(
    raw_root: Path, snapshot_ts: str, host: str, since: date, outputs: dict[str, CommandOutput]
) -> Path:
    """Gzip every command's output next to a small metadata file, so `reparse` can replay it."""
    day_dir = raw_root / snapshot_ts[:10]
    day_dir.mkdir(parents=True, exist_ok=True)
    stem = compact_timestamp(snapshot_ts)

    for name, output in outputs.items():
        with gzip.open(day_dir / f"{stem}_{name}.txt.gz", "wt", encoding="utf-8") as handle:
            handle.write(output.stdout)

    metadata = {
        "snapshot_ts": snapshot_ts,
        "host": host,
        "since": since.isoformat(),
        "commands": {name: output.command for name, output in outputs.items()},
        "exit_codes": {name: output.exit_code for name, output in outputs.items()},
    }
    (day_dir / f"{stem}_meta.json").write_text(json.dumps(metadata, indent=2))

    return day_dir


def load_raw_runs(raw_root: Path) -> list[tuple[str, dict[str, CommandOutput]]]:
    """All stored runs in chronological order, as (snapshot_ts, outputs)."""
    runs = []

    for meta_path in sorted(raw_root.glob("*/*_meta.json")):
        metadata = json.loads(meta_path.read_text())
        stem = meta_path.name.removesuffix("_meta.json")
        outputs = {}

        for name, command in metadata["commands"].items():
            raw_path = meta_path.parent / f"{stem}_{name}.txt.gz"

            if not raw_path.exists():
                continue

            with gzip.open(raw_path, "rt", encoding="utf-8") as handle:
                outputs[name] = CommandOutput(
                    name=name,
                    command=command,
                    stdout=handle.read(),
                    exit_code=metadata["exit_codes"].get(name),
                )

        runs.append((metadata["snapshot_ts"], outputs))

    return sorted(runs, key=lambda run: run[0])


def successful_output(
    outputs: dict[str, CommandOutput], name: str, errors: list[str]
) -> str | None:
    output = outputs.get(name)

    if output is None:
        errors.append(f"{name}: no output captured")
        return None

    if output.exit_code != 0:
        errors.append(f"{name}: remote exit code {output.exit_code}")
        return None

    return output.stdout


def ingest_outputs(
    config: Config, store: Store, snapshot_ts: str, outputs: dict[str, CommandOutput]
) -> IngestResult:
    """Parse and store one run; each collector fails independently and is reported."""
    result = IngestResult(snapshot_ts=snapshot_ts)

    ingest_jobs(config, store, outputs, result)
    bsc_acct_report = ingest_bsc_acct(store, outputs, result)
    ingest_visibility(config, store, outputs, bsc_acct_report, result)

    if "quota" in outputs:
        ingest_quota(store, outputs, result)

    result.attribution_changes = refresh_attributions(config, store)

    if bsc_acct_report:
        result.reconciliation = reconcile_own_usage(config, store, bsc_acct_report)
        result.warnings.extend(reconciliation_warnings(result.reconciliation))

    return result


def ingest_jobs(
    config: Config, store: Store, outputs: dict[str, CommandOutput], result: IngestResult
) -> None:
    text = successful_output(outputs, "sacct", result.errors)

    if text is None:
        return

    try:
        jobs = parse_sacct(text)
    except ParseError as error:
        result.errors.append(f"sacct: {error}")
        return

    records = []
    spec_warnings = []

    for job in jobs:
        usage = compute_usage(job, config.node_types)
        attribution = attribute(job, config.attribution_rules)
        records.append(JobRecord.from_parts(job, usage, attribution, result.snapshot_ts))

        if warning := check_node_spec(job, usage, config.node_types):
            spec_warnings.append(warning)

    if spec_warnings:
        result.warnings.append(
            f"{len(spec_warnings)} jobs contradict node_types, e.g. {spec_warnings[0]}"
        )

    known_ids = {record.job_id for record in store.load_jobs()}
    counts = store.upsert_jobs(records)
    result.jobs_seen = len(records)
    result.jobs_new = counts.new
    result.jobs_updated = counts.updated
    result.new_jobs = [
        {"job_id": record.job_id, "job_name": record.job_name, "account": record.account}
        for record in records
        if record.job_id not in known_ids
    ][:MAX_LISTED_NEW_JOBS]


def ingest_bsc_acct(
    store: Store, outputs: dict[str, CommandOutput], result: IngestResult
) -> BscAcctReport | None:
    text = successful_output(outputs, "bsc_acct", result.errors)

    if text is None:
        return None

    try:
        report = parse_bsc_acct(text)
    except ParseError as error:
        result.errors.append(f"bsc_acct: {error}")
        return None

    result.budget_rows, result.user_rows = store.insert_bsc_acct(result.snapshot_ts, report)

    return report


def ingest_visibility(
    config: Config,
    store: Store,
    outputs: dict[str, CommandOutput],
    bsc_acct_report: BscAcctReport | None,
    result: IngestResult,
) -> None:
    """Record which allocations are visible; skipped unless all three sources succeeded,
    because a failed source would otherwise look like lost membership."""
    association_text = successful_output(outputs, "associations", result.errors)
    groups_text = successful_output(outputs, "groups", result.errors)

    if association_text is None or groups_text is None or bsc_acct_report is None:
        result.warnings.append("visibility snapshot skipped: a source failed")
        return

    try:
        associations = {row.account: row for row in parse_associations(association_text)}
        groups = set(parse_groups(groups_text))
    except ParseError as error:
        result.errors.append(f"associations: {error}")
        return

    bsc_acct_accounts = set(bsc_acct_report.accounts)
    accounts = set(config.allocations) | set(associations) | bsc_acct_accounts
    rows = [
        AssociationSnapshotRow(
            snapshot_ts=result.snapshot_ts,
            account=account,
            partitions=associations[account].partitions if account in associations else "",
            qos=associations[account].qos if account in associations else "",
            has_association=account in associations,
            in_unix_group=account in groups,
            in_bsc_acct=account in bsc_acct_accounts,
        )
        for account in sorted(accounts)
    ]

    previous_timestamps = store.snapshot_timestamps("associations_snapshots")
    previous = store.association_snapshots(previous_timestamps[-1]) if previous_timestamps else []
    result.association_rows = store.insert_associations(rows)
    result.visibility_changes = diff_visibility(previous, rows)


def diff_visibility(
    previous: list[AssociationSnapshotRow], current: list[AssociationSnapshotRow]
) -> list[VisibilityChange]:
    if not previous:
        return []

    before = {row.account: row for row in previous}
    changes = []

    for row in current:
        old = before.get(row.account)

        for source in ("has_association", "in_unix_group", "in_bsc_acct"):
            old_value = bool(getattr(old, source)) if old else False
            new_value = bool(getattr(row, source))

            if old_value != new_value:
                changes.append(VisibilityChange(row.account, source, old_value, new_value))

    return changes


def ingest_quota(store: Store, outputs: dict[str, CommandOutput], result: IngestResult) -> None:
    text = successful_output(outputs, "quota", result.errors)

    if text is None:
        return

    try:
        result.quota_rows = store.insert_quota(result.snapshot_ts, parse_quota(text))
    except ParseError as error:
        result.errors.append(f"quota: {error}")


def refresh_attributions(config: Config, store: Store) -> int:
    jobs = store.load_jobs()

    return store.update_attributions(
        {job.job_id: attribute(job, config.attribution_rules) for job in jobs}
    )


def reconcile_own_usage(
    config: Config, store: Store, report: BscAcctReport
) -> list[Reconciliation]:
    sacct_khours: defaultdict[tuple[str, str], float] = defaultdict(float)

    for job in store.load_jobs():
        if job.partition_class:
            sacct_khours[(job.account, job.partition_class)] += job.core_hours_billed / 1000

    rows = []

    for usage in report.user_usage:
        node_type = config.node_type_for_machine(usage.machine)

        if usage.user != config.user or node_type is None:
            continue

        ours = sacct_khours[(usage.account, node_type.name)]

        if max(ours, usage.used_khours) < RECONCILIATION_MIN_KHOURS:
            continue

        diff_pct = (
            (ours - usage.used_khours) / usage.used_khours * 100 if usage.used_khours else None
        )
        rows.append(
            Reconciliation(
                account=usage.account,
                machine=usage.machine,
                sacct_khours=round(ours, 2),
                bsc_acct_khours=usage.used_khours,
                diff_pct=round(diff_pct, 1) if diff_pct is not None else None,
            )
        )

    return rows


def reconciliation_warnings(rows: list[Reconciliation]) -> list[str]:
    return [
        f"{row.account} {row.machine}: sacct {row.sacct_khours} vs bsc_acct {row.bsc_acct_khours} "
        f"khours ({row.diff_pct:+.1f} %); check node_types or sacct history window"
        for row in rows
        if row.diff_pct is None or abs(row.diff_pct) > RECONCILIATION_WARN_PCT
    ]


def run_sync(
    config: Config, store: Store, runner: RemoteRunner, since: date | None, full: bool
) -> SyncSummary:
    started = time.monotonic()
    snapshot_ts = utc_now()
    window_start = determine_since(store, config, since, full)
    commands = build_commands(config, window_start)

    try:
        batch = runner.run_batch(commands)
    except RemoteError as error:
        result = IngestResult(snapshot_ts=snapshot_ts, errors=[str(error)])
        summary = SyncSummary(
            None, window_start.isoformat(), time.monotonic() - started, [], result, None
        )
        log_run(store, "sync", summary)
        return summary

    raw_dir = save_raw_outputs(store.raw_dir, snapshot_ts, batch.host, window_start, batch.outputs)
    result = ingest_outputs(config, store, snapshot_ts, batch.outputs)
    summary = SyncSummary(
        host=batch.host,
        since=window_start.isoformat(),
        duration_s=round(time.monotonic() - started, 1),
        failed_attempts=batch.failed_attempts,
        result=result,
        raw_dir=str(raw_dir),
    )
    log_run(store, "sync", summary)

    return summary


def run_reparse(config: Config, store: Store) -> list[IngestResult]:
    """Rebuild jobs and snapshots from every stored raw run, oldest first."""
    started = time.monotonic()
    runs = load_raw_runs(store.raw_dir)
    store.clear_for_reparse()
    results = [ingest_outputs(config, store, snapshot_ts, outputs) for snapshot_ts, outputs in runs]

    combined = IngestResult(
        snapshot_ts=utc_now(),
        jobs_seen=sum(result.jobs_seen for result in results),
        jobs_new=sum(result.jobs_new for result in results),
        errors=[f"{result.snapshot_ts}: {error}" for result in results for error in result.errors],
    )
    log_run(store, "reparse", SyncSummary(None, "", time.monotonic() - started, [], combined, None))

    return results


def log_run(store: Store, kind: str, summary: SyncSummary) -> None:
    result = summary.result
    store.write_sync_log(
        SyncLogEntry(
            started_at=result.snapshot_ts,
            finished_at=utc_now(),
            kind=kind,
            host=summary.host,
            duration_s=round(summary.duration_s, 1),
            since=summary.since or None,
            jobs_seen=result.jobs_seen,
            jobs_new=result.jobs_new,
            jobs_updated=result.jobs_updated,
            budget_rows=result.budget_rows,
            user_rows=result.user_rows,
            association_rows=result.association_rows,
            quota_rows=result.quota_rows,
            errors=result.errors,
            warnings=summary.failed_attempts + result.warnings,
            ok=not result.errors,
        )
    )
