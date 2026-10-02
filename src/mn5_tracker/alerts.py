"""Budget, expiry, exhaustion, visibility-loss and stale-data checks."""

from dataclasses import dataclass
from datetime import UTC, date, datetime

from mn5_tracker.allocation_status import AllocationStatus
from mn5_tracker.config import Config
from mn5_tracker.store import Store
from mn5_tracker.sync import diff_visibility


@dataclass(frozen=True)
class Alert:
    severity: str
    kind: str
    account: str | None
    message: str


def evaluate_alerts(
    config: Config, store: Store, statuses: list[AllocationStatus], today: date
) -> list[Alert]:
    alerts = [*stale_sync_alerts(config, store, today)]

    for status in statuses:
        if status.visible_in_bsc_acct:
            alerts.extend(budget_alerts(config, status))

    alerts.extend(visibility_alerts(store))

    severity_order = {"critical": 0, "warning": 1}

    return sorted(alerts, key=lambda alert: (severity_order.get(alert.severity, 2), alert.kind))


def budget_alerts(config: Config, status: AllocationStatus) -> list[Alert]:
    thresholds = config.alerts
    label = f"{status.account} {status.machine}"
    alerts = []

    if status.used_pct is not None and status.used_pct >= thresholds.used_pct_warn:
        severity = "critical" if status.used_pct >= 95 else "warning"
        alerts.append(
            Alert(severity, "budget", status.account, f"{label} is {status.used_pct:.0f}% used")
        )

    if status.days_to_expiry is not None and status.days_to_expiry < 0:
        alerts.append(
            Alert(
                "warning",
                "expired",
                status.account,
                f"{label} expired on {status.expiration_date} but is still listed in bsc_acct",
            )
        )
    elif status.days_to_expiry is not None and status.days_to_expiry <= thresholds.expiry_days_warn:
        unused = (
            f", {100 - (status.used_pct or 0):.0f}% unused" if status.used_pct is not None else ""
        )
        alerts.append(
            Alert(
                "critical" if status.days_to_expiry <= 7 else "warning",
                "expiry",
                status.account,
                f"{label} expires on {status.expiration_date} "
                f"(in {status.days_to_expiry} days{unused})",
            )
        )

    if status.runs_out_before_expiry:
        alerts.append(
            Alert(
                "warning",
                "exhaustion",
                status.account,
                f"{label} projected to run out on {status.projected_exhaustion}, before expiry "
                f"{status.expiration_date} ({status.burn_rate_source})",
            )
        )

    return alerts


def visibility_alerts(store: Store) -> list[Alert]:
    """Accounts that disappeared from sacctmgr, `id -Gn` or bsc_acct since the previous sync."""
    timestamps = store.snapshot_timestamps("associations_snapshots")

    if len(timestamps) < 2:
        return []

    previous = store.association_snapshots(timestamps[-2])
    current = store.association_snapshots(timestamps[-1])
    source_labels = {
        "has_association": "Slurm associations",
        "in_unix_group": "Unix groups (id -Gn)",
        "in_bsc_acct": "bsc_acct",
    }

    return [
        Alert(
            "critical",
            "visibility",
            change.account,
            f"{change.account} disappeared from {source_labels[change.source]} since "
            f"{timestamps[-2][:10]}; the last snapshot is now its permanent record",
        )
        for change in diff_visibility(previous, current)
        if change.before and not change.after
    ]


def stale_sync_alerts(config: Config, store: Store, today: date) -> list[Alert]:
    last_sync = store.last_successful_sync()

    if last_sync is None:
        return [Alert("critical", "stale", None, "No successful sync yet: run `mn5track sync`")]

    age_days = (
        datetime.combine(today, datetime.min.time(), UTC) - datetime.fromisoformat(last_sync)
    ).days

    if age_days < config.alerts.stale_sync_days:
        return []

    return [
        Alert(
            "warning",
            "stale",
            None,
            f"Last successful sync was {last_sync[:10]} ({age_days} days ago); bsc_acct "
            "snapshots may miss an allocation's final state",
        )
    ]
