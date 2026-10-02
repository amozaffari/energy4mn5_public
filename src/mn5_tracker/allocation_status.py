"""Per-allocation budget state: remaining budget, burn rate, projected exhaustion, visibility."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from mn5_tracker.config import Config
from mn5_tracker.metrics import khours_to_node_hours
from mn5_tracker.store import AssociationSnapshotRow, JobRecord, Store


BURN_WINDOW_DAYS = 30
MIN_SNAPSHOT_SPAN_DAYS = 1.0


@dataclass(frozen=True)
class AllocationStatus:
    account: str
    title: str
    kind: str
    machine: str
    node_class: str
    total_khours: float | None
    used_khours: float
    used_pct: float | None
    remaining_khours: float | None
    total_node_hours: float | None
    used_node_hours: float
    remaining_node_hours: float | None
    gpus_per_node: int
    expiration_date: date | None
    days_to_expiry: int | None
    burn_khours_per_day: float | None
    burn_rate_source: str
    projected_exhaustion: date | None
    runs_out_before_expiry: bool | None
    snapshot_ts: str
    accounting_updated: str | None
    visible_in_bsc_acct: bool
    in_unix_group: bool | None
    has_association: bool | None

    @property
    def is_final_snapshot(self) -> bool:
        """True when the allocation vanished from bsc_acct: its last snapshot is permanent."""
        return not self.visible_in_bsc_acct


def snapshot_date(snapshot_ts: str) -> datetime:
    return datetime.fromisoformat(snapshot_ts)


def snapshot_burn_rate(history: list[dict[str, Any]], today: date) -> float | None:
    """khours/day between the oldest and newest snapshot in the burn window (all users)."""
    window_start = today - timedelta(days=BURN_WINDOW_DAYS)
    recent = [row for row in history if snapshot_date(row["snapshot_ts"]).date() >= window_start]

    if len(recent) < 2:
        return None

    first, last = recent[0], recent[-1]
    span_days = (
        snapshot_date(last["snapshot_ts"]) - snapshot_date(first["snapshot_ts"])
    ).total_seconds() / 86400

    if span_days < MIN_SNAPSHOT_SPAN_DAYS:
        return None

    return max(last["used_khours"] - first["used_khours"], 0.0) / span_days


def own_jobs_burn_rate(jobs: list[JobRecord], account: str, node_class: str, today: date) -> float:
    window_start = today - timedelta(days=BURN_WINDOW_DAYS)
    khours = sum(
        job.core_hours_billed / 1000
        for job in jobs
        if job.account == account
        and job.partition_class == node_class
        and job.start_date
        and job.start_date >= window_start
    )

    return khours / BURN_WINDOW_DAYS


def project_exhaustion(
    remaining_khours: float | None, burn_per_day: float | None, today: date
) -> date | None:
    if remaining_khours is None or not burn_per_day:
        return None

    return today + timedelta(days=remaining_khours / burn_per_day)


def compute_statuses(config: Config, store: Store, today: date) -> list[AllocationStatus]:
    """One status per (account, MN5 machine) that has a budget or any recorded usage."""
    latest_per_account = store.last_budget_snapshot_per_account()
    budget_timestamps = store.snapshot_timestamps("budget_snapshots")
    latest_budget_ts = budget_timestamps[-1] if budget_timestamps else None
    association_timestamps = store.snapshot_timestamps("associations_snapshots")
    associations = {
        row.account: row
        for row in (
            store.association_snapshots(association_timestamps[-1])
            if association_timestamps
            else []
        )
    }
    jobs = store.load_jobs()
    statuses = []

    for account, rows in latest_per_account.items():
        history = store.budget_snapshots(account)

        for row in rows:
            node_type = config.node_type_for_machine(row["machine"])

            if node_type is None or (row["total_khours"] is None and not row["used_khours"]):
                continue

            machine_history = [entry for entry in history if entry["machine"] == row["machine"]]
            statuses.append(
                build_status(
                    config=config,
                    row=row,
                    machine_history=machine_history,
                    jobs=jobs,
                    node_class=node_type.name,
                    today=today,
                    visible=row["snapshot_ts"] == latest_budget_ts,
                    association=associations.get(account),
                )
            )

    return sorted(statuses, key=lambda status: (not status.visible_in_bsc_acct, status.account))


def build_status(
    config: Config,
    row: dict[str, Any],
    machine_history: list[dict[str, Any]],
    jobs: list[JobRecord],
    node_class: str,
    today: date,
    visible: bool,
    association: AssociationSnapshotRow | None,
) -> AllocationStatus:
    node_type = config.node_types[node_class]
    account = row["account"]
    total = row["total_khours"]
    used = row["used_khours"]
    remaining = total - used if total is not None else None
    expiration = date.fromisoformat(row["expiration_date"]) if row["expiration_date"] else None

    burn = snapshot_burn_rate(machine_history, today)
    burn_source = f"bsc_acct snapshots, last {BURN_WINDOW_DAYS} d (all users)"

    if burn is None:
        burn = own_jobs_burn_rate(jobs, account, node_class, today)
        burn_source = f"own sacct jobs, last {BURN_WINDOW_DAYS} d (lower bound)"

    if not visible:
        burn_source = "not visible: final snapshot"

    exhaustion = project_exhaustion(remaining, burn, today) if visible else None
    allocation = config.allocations.get(account)

    return AllocationStatus(
        account=account,
        title=allocation.title if allocation else row["project_title"],
        kind=allocation.kind if allocation else "unknown",
        machine=row["machine"],
        node_class=node_class,
        total_khours=total,
        used_khours=used,
        used_pct=used / total * 100 if total else None,
        remaining_khours=remaining,
        total_node_hours=khours_to_node_hours(total, node_type) if total is not None else None,
        used_node_hours=khours_to_node_hours(used, node_type),
        remaining_node_hours=(
            khours_to_node_hours(remaining, node_type) if remaining is not None else None
        ),
        gpus_per_node=node_type.gpus,
        expiration_date=expiration,
        days_to_expiry=(expiration - today).days if expiration else None,
        burn_khours_per_day=round(burn, 3) if burn is not None else None,
        burn_rate_source=burn_source,
        projected_exhaustion=exhaustion,
        runs_out_before_expiry=(
            exhaustion < expiration if exhaustion and expiration and visible else None
        ),
        snapshot_ts=row["snapshot_ts"],
        accounting_updated=row["accounting_updated"],
        visible_in_bsc_acct=visible,
        in_unix_group=association.in_unix_group if association else None,
        has_association=association.has_association if association else None,
    )
