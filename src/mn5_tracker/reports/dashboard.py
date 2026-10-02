"""One-page overall-usage dashboard: a self-contained HTML file with the data embedded as JSON."""

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from mn5_tracker.aggregation import (
    WASTED_STATES,
    AllocationUsage,
    load_attributed_jobs,
    project_lifetime,
    total,
)
from mn5_tracker.alerts import evaluate_alerts
from mn5_tracker.allocation_status import AllocationStatus, compute_statuses
from mn5_tracker.attribution import UNATTRIBUTED
from mn5_tracker.config import Config
from mn5_tracker.contributions import Contribution, energy_inputs, load_contributions
from mn5_tracker.energy import (
    EnergyEstimate,
    EnergyInput,
    PowerModel,
    assumptions,
    build_power_model,
    estimate,
    estimate_by,
    inputs_from_jobs,
)
from mn5_tracker.store import JobRecord, Store, utc_now


DASHBOARD_SCHEMA = "mn5track.dashboard/v1"
TEMPLATE_PATH = Path(__file__).with_name("dashboard_template.html")
OUTCOMES = ("completed", "timeout", "wasted", "debug_failed", "active")


@dataclass(frozen=True)
class UsagePoint:
    """One aggregated slice of billed usage, from a local job or a contribution row."""

    month: str
    account: str
    project: str
    partition_class: str
    state: str
    jobs: int
    billed_node_hours: float
    queue: str = "production"


def outcome(state: str, queue: str) -> str:
    """Failures in development (debug/interactive) QOS are expected, so they are not waste."""
    if state == "COMPLETED":
        return "completed"

    if state == "TIMEOUT":
        return "timeout"

    if state in WASTED_STATES:
        return "debug_failed" if queue == "development" else "wasted"

    return "active"


def points_from_jobs(jobs: list[JobRecord]) -> list[UsagePoint]:
    return [
        UsagePoint(
            month=job.start_date.strftime("%Y-%m"),
            account=job.account,
            project=job.project,
            partition_class=job.partition_class,
            state=job.state,
            jobs=1,
            billed_node_hours=job.billed_node_hours,
            queue=job.queue,
        )
        for job in jobs
        if job.start_date and job.partition_class
    ]


def points_from_contributions(contributions: list[Contribution]) -> list[UsagePoint]:
    return [
        UsagePoint(
            month=row.month,
            account=row.account,
            project=contribution.project,
            partition_class=row.partition_class,
            state=row.state,
            jobs=row.jobs,
            billed_node_hours=row.billed_node_hours,
            queue=row.queue,
        )
        for contribution in contributions
        for row in contribution.rows
        if row.month[:4].isdigit()
    ]


def month_range(months: list[str]) -> list[str]:
    """Every month from the first to the last, so quiet months show as gaps, not jumps."""
    if not months:
        return []

    year, month = map(int, min(months).split("-"))
    last = max(months)
    filled = []

    while f"{year:04d}-{month:02d}" <= last:
        filled.append(f"{year:04d}-{month:02d}")
        month += 1

        if month == 13:
            year, month = year + 1, 1

    return filled


def monthly_by(points: list[UsagePoint], key: str) -> list[dict[str, Any]]:
    totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    for point in points:
        totals[point.month][getattr(point, key)] += point.billed_node_hours

    return [
        {"month": month, "values": {name: round(value, 2) for name, value in totals[month].items()}}
        for month in month_range(list(totals))
    ]


def monthly_outcomes(points: list[UsagePoint]) -> list[dict[str, Any]]:
    totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    for point in points:
        totals[point.month][outcome(point.state, point.queue)] += point.billed_node_hours

    return [
        {
            "month": month,
            "values": {name: round(totals[month].get(name, 0.0), 2) for name in OUTCOMES},
        }
        for month in month_range(list(totals))
    ]


def outcome_counts(points: list[UsagePoint]) -> dict[str, dict[str, float]]:
    counts: dict[str, dict[str, float]] = {
        name: {"jobs": 0, "billed_node_hours": 0.0} for name in OUTCOMES
    }

    for point in points:
        bucket = counts[outcome(point.state, point.queue)]
        bucket["jobs"] += point.jobs
        bucket["billed_node_hours"] += point.billed_node_hours

    return counts


def ordered_series(config: Config, names: set[str], kind: str) -> list[str]:
    """Stable order so a project or allocation keeps its colour: config order, then the rest."""
    configured = list(config.projects if kind == "project" else config.allocations)
    ordered = [name for name in configured if name in names]
    ordered += sorted(name for name in names if name not in ordered and name != UNATTRIBUTED)

    if UNATTRIBUTED in names:
        ordered.append(UNATTRIBUTED)

    return ordered


def allocation_panels(
    config: Config, statuses: list[AllocationStatus], jobs: list[JobRecord], store: Store
) -> list[dict[str, Any]]:
    panels = []

    for status in statuses:
        if not status.visible_in_bsc_acct or not status.total_khours:
            continue

        daily: dict[str, float] = defaultdict(float)

        for job in jobs:
            if (
                job.account == status.account
                and job.partition_class == status.node_class
                and job.end
            ):
                daily[job.end[:10]] += job.billed_node_hours

        cumulative = 0.0
        own_series = []

        for day in sorted(daily):
            cumulative += daily[day]
            own_series.append({"date": day, "value": round(cumulative, 2)})

        node_type = config.node_types[status.node_class]
        snapshots = [
            {
                "date": row["snapshot_ts"][:10],
                "value": round(row["used_khours"] * 1000 / node_type.physical_cores, 2),
            }
            for row in store.budget_snapshots(status.account)
            if row["machine"] == status.machine
        ]
        allocation = config.allocations.get(status.account)
        panels.append(
            {
                "account": status.account,
                "title": status.title,
                "node_class": status.node_class,
                "budget_node_hours": round(status.total_node_hours or 0, 1),
                "used_node_hours": round(status.used_node_hours, 1),
                "used_pct": round(status.used_pct or 0, 1),
                "expiration_date": status.expiration_date.isoformat()
                if status.expiration_date
                else None,
                "days_to_expiry": status.days_to_expiry,
                "projected_exhaustion": (
                    status.projected_exhaustion.isoformat() if status.projected_exhaustion else None
                ),
                "start": allocation.start.isoformat() if allocation and allocation.start else None,
                "own_series": own_series,
                "snapshots": snapshots,
            }
        )

    return panels


ASSUMPTION_LABELS = {
    "pue": ("PUE (data-centre overhead)", "pue"),
    "acc_node_power_w": ("ACC node power, W (low / central / high)", "acc_gpu_utilisation"),
    "gpp_node_power_w": ("GPP node power, W", "gpp_node_power_w"),
    "grid_g_per_kwh": ("Grid intensity, g CO₂e/kWh (location-based)", "grid"),
    "market_g_per_kwh": ("Market-based intensity, g CO₂e/kWh", "grid"),
    "embodied_kg_per_node_hour": ("Embodied carbon, kg CO₂e per node-hour", None),
}


UNUSED_HINTS = {
    "market_g_per_kwh": "Set energy.grid.market_g_per_kwh if electricity is certified renewable",
    "embodied_kg_per_node_hour": "Optional scope 3: set energy.embodied_kg_per_node_hour",
}


def assumption_rows(config: Config, power: PowerModel) -> list[dict[str, str]]:
    """The model's parameters as a readable table, each with its source note."""
    values = assumptions(config, power)
    sources = config.energy.sources
    rows = []

    for key, (label, source_key) in ASSUMPTION_LABELS.items():
        value = values[key]

        if isinstance(value, dict):
            value = " / ".join(f"{item:g}" for item in value.values()) if value else None
        elif isinstance(value, float):
            value = f"{value:g}"

        if key == "gpp_node_power_w":
            source = power.gpp_calibration
        elif key == "acc_node_power_w":
            settings = config.energy
            utilisation = " / ".join(f"{u:g}" for u in settings.acc_gpu_utilisation.values())
            source = (
                f"4 × {settings.acc_gpu_power_w:g} W GPU × utilisation {utilisation} "
                f"+ {settings.acc_host_power_w:g} W host. "
                f"{sources.get('acc_gpu_utilisation', '')}"
            )
        elif value is None:
            source = UNUSED_HINTS.get(key, "")
        else:
            source = sources.get(source_key or "", "")

        rows.append(
            {
                "parameter": label,
                "value": "not used" if value is None else str(value),
                "source": source,
            }
        )

    return rows


def group_estimates(
    config: Config, power: PowerModel, groups: dict[str, list[EnergyInput]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    for name, items in groups.items():
        result = estimate(items, config.energy, power)

        if result.billed_node_hours < 0.5:
            continue

        rows.append(
            {
                "name": name,
                "value": round(result.co2e_kg["central"], 1),
                "low": round(result.co2e_kg["low"], 1),
                "high": round(result.co2e_kg["high"], 1),
                "facility_kwh": round(result.facility_kwh["central"], 1),
            }
        )

    return sorted(rows, key=lambda row: -row["value"])


def inputs_by_queue(config: Config, jobs: list[JobRecord]) -> dict[str, list[EnergyInput]]:
    grouped: dict[str, list[EnergyInput]] = defaultdict(list)

    for job in jobs:
        grouped[job.queue].extend(inputs_from_jobs([job], config))

    return dict(grouped)


def carbon_section(
    config: Config,
    monthly_inputs: list[EnergyInput],
    headline: EnergyEstimate,
    power_inputs: list[EnergyInput],
    groups: dict[str, list[EnergyInput]],
    queue_groups: dict[str, list[EnergyInput]],
) -> dict[str, Any]:
    """Monthly kg CO2e per partition, per-group and per-queue totals, measured share and the
    assumptions."""
    power = build_power_model(config, power_inputs)

    by_month_class = estimate_by(
        [item for item in monthly_inputs if item.month],
        config.energy,
        power,
        key=lambda item: f"{item.month}|{item.partition_class}",
    )
    monthly: dict[str, dict[str, float]] = defaultdict(dict)
    ranges: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])

    for key, result in by_month_class.items():
        month, partition_class = key.split("|")
        monthly[month][partition_class] = round(result.co2e_kg["central"], 2)
        ranges[month][0] += result.co2e_kg["low"]
        ranges[month][1] += result.co2e_kg["high"]

    return {
        "verified": config.energy.verified,
        "pue": config.energy.pue,
        "grid_g_per_kwh": config.energy.grid_g_per_kwh,
        "gpp_calibration": power.gpp_calibration,
        "facility_kwh": {name: round(value, 1) for name, value in headline.facility_kwh.items()},
        "co2e_kg": {name: round(value, 1) for name, value in headline.co2e_kg.items()},
        "measured_share": headline.measured_share,
        "measured_it_kwh": round(headline.measured_it_kwh, 1),
        "modelled_it_kwh": {
            name: round(value - headline.measured_it_kwh, 1)
            for name, value in headline.it_kwh.items()
        },
        "by_group": group_estimates(config, power, groups),
        "by_queue": group_estimates(config, power, queue_groups),
        "assumptions": assumption_rows(config, power),
        "monthly": [
            {
                "month": month,
                "values": monthly.get(month, {}),
                "range": [round(value, 1) for value in ranges.get(month, [0.0, 0.0])],
            }
            for month in month_range(list(monthly))
        ],
    }


def project_inputs_by_account(
    config: Config,
    project_jobs: list[JobRecord],
    contributions: list[Contribution],
    allocations: list[AllocationUsage],
) -> dict[str, list[EnergyInput]]:
    """Per allocation: own jobs + contribution rows + bsc_acct fallback, as energy inputs."""
    grouped: dict[str, list[EnergyInput]] = defaultdict(list)

    for job in project_jobs:
        grouped[job.account].extend(inputs_from_jobs([job], config))

    for contribution in contributions:
        for item, row in zip(energy_inputs([contribution]), contribution.rows, strict=True):
            grouped[row.account].append(item)

    for allocation in allocations:
        for node_class, hours in (
            allocation.bsc_acct_others_billed_node_hours_by_class or {}
        ).items():
            grouped[allocation.account].append(EnergyInput(None, node_class, hours))

    return dict(grouped)


NO_QUEUE = "not split (bsc_acct)"


def project_inputs_by_queue(
    config: Config,
    project_jobs: list[JobRecord],
    contributions: list[Contribution],
    allocations: list[AllocationUsage],
) -> dict[str, list[EnergyInput]]:
    """Production vs development for the project; bsc_acct totals carry no QOS."""
    grouped = defaultdict(list, inputs_by_queue(config, project_jobs))

    for contribution in contributions:
        for item, row in zip(energy_inputs([contribution]), contribution.rows, strict=True):
            grouped[row.queue].append(item)

    for allocation in allocations:
        for node_class, hours in (
            allocation.bsc_acct_others_billed_node_hours_by_class or {}
        ).items():
            grouped[NO_QUEUE].append(EnergyInput(None, node_class, hours))

    return dict(grouped)


def build_dashboard_data(
    config: Config, store: Store, today: date, project: str | None = None
) -> dict[str, Any]:
    """Everything the page shows, computed with the same functions as the CLI commands."""
    jobs = load_attributed_jobs(config, store)
    statuses = compute_statuses(config, store, today)
    alerts = evaluate_alerts(config, store, statuses, today)
    gpus_per_acc_node = config.node_types["acc"].gpus if "acc" in config.node_types else 0

    if project:
        project_jobs = [job for job in jobs if job.project == project]
        contributions = load_contributions(store, project, config.user)
        points = points_from_jobs(project_jobs) + points_from_contributions(contributions)
        lifetime = project_lifetime(config, store, jobs, project)
        lower_bound = lifetime.lower_bound_billed_node_hours_by_class
        headline_node_hours = lower_bound.get("acc", 0.0)
        share: list[dict[str, Any]] = [
            {
                "name": allocation.account,
                "value": round(sum(allocation.total_billed_node_hours_by_class.values()), 1),
            }
            for allocation in lifetime.allocations
            if sum(allocation.total_billed_node_hours_by_class.values()) >= 0.5
        ]
        scope = {
            "kind": "project",
            "project": project,
            "title": lifetime.title,
            "label": f"{lifetime.title}: your jobs, imported contributions and bsc_acct totals",
            "coverage": {
                "members_with_detail": lifetime.members_with_detail,
                "members_from_bsc_acct_only": lifetime.members_from_bsc_acct_only,
                "contributors": [contributor.contributor for contributor in lifetime.contributors],
                "invisible_allocations": lifetime.invisible,
            },
        }
        group_keys = ["account", "queue"]
        share_kind = "account"
        carbon = carbon_section(
            config,
            inputs_from_jobs(project_jobs, config) + energy_inputs(contributions),
            lifetime.energy,
            inputs_from_jobs(jobs, config),
            project_inputs_by_account(config, project_jobs, contributions, lifetime.allocations),
            project_inputs_by_queue(config, project_jobs, contributions, lifetime.allocations),
        )
    else:
        points = points_from_jobs(jobs)
        own_total = total(jobs)
        headline_node_hours = own_total.acc_billed_node_hours
        lower_bound = {
            "acc": own_total.acc_billed_node_hours,
            "gpp": own_total.gpp_billed_node_hours,
        }
        project_totals: dict[str, float] = defaultdict(float)

        for point in points:
            project_totals[point.project] += point.billed_node_hours

        share = [{"name": name, "value": round(value, 1)} for name, value in project_totals.items()]
        scope = {
            "kind": "own",
            "project": None,
            "title": f"{config.user}",
            "label": "Your own jobs across all projects and allocations (sacct)",
            "coverage": None,
        }
        group_keys = ["project", "account", "queue"]
        share_kind = "project"
        own_inputs = inputs_from_jobs(jobs, config)
        carbon = carbon_section(
            config,
            own_inputs,
            estimate(own_inputs, config.energy, build_power_model(config, own_inputs)),
            own_inputs,
            {
                name: inputs_from_jobs([job for job in jobs if job.project == name], config)
                for name in {job.project for job in jobs}
            },
            inputs_by_queue(config, jobs),
        )

    counts = outcome_counts(points)
    finished_jobs = sum(
        counts[name]["jobs"] for name in ("completed", "timeout", "wasted", "debug_failed")
    )
    production_points = [point for point in points if point.queue != "development"]
    production_counts = outcome_counts(production_points)
    production_finished = sum(
        production_counts[name]["jobs"] for name in ("completed", "timeout", "wasted")
    )
    production_node_hours = sum(point.billed_node_hours for point in production_points)
    panels = allocation_panels(config, statuses, jobs, store)
    series_names = {
        "project": ordered_series(config, {point.project for point in points}, "project"),
        "account": ordered_series(
            config,
            {point.account for point in points} | {panel["account"] for panel in panels},
            "account",
        ),
        "partition": [name for name in config.node_types],
        "queue": ["production", "development"],
    }

    return {
        "schema": DASHBOARD_SCHEMA,
        "generated_at": utc_now(),
        "today": today.isoformat(),
        "scope": scope,
        "tiles": {
            "gpu_hours_billed": round(headline_node_hours * gpus_per_acc_node, 0),
            "acc_node_hours": round(lower_bound.get("acc", 0.0), 0),
            "gpp_node_hours": round(lower_bound.get("gpp", 0.0), 0),
            "success_rate": (
                round(counts["completed"]["jobs"] / finished_jobs, 3) if finished_jobs else None
            ),
            "production_success_rate": (
                round(production_counts["completed"]["jobs"] / production_finished, 3)
                if production_finished
                else None
            ),
            "wasted_share": (
                round(counts["wasted"]["billed_node_hours"] / production_node_hours, 3)
                if production_node_hours
                else None
            ),
            "debug_failed_node_hours": round(counts["debug_failed"]["billed_node_hours"], 1),
            "debug_failed_jobs": int(counts["debug_failed"]["jobs"]),
            "jobs": int(sum(bucket["jobs"] for bucket in counts.values())),
            "active_allocations": sum(
                1 for status in statuses if status.visible_in_bsc_acct and status.total_khours
            ),
        },
        "series": series_names,
        "group_keys": group_keys,
        "monthly": {key: monthly_by(points, key) for key in group_keys},
        "share": {"kind": share_kind, "rows": sorted(share, key=lambda row: -float(row["value"]))},
        "outcomes": {"monthly": monthly_outcomes(points), "totals": counts},
        "allocations": panels,
        "carbon": carbon,
        "health": {
            "last_sync": store.last_successful_sync(),
            "alerts": [
                {"severity": alert.severity, "kind": alert.kind, "message": alert.message}
                for alert in alerts
            ],
        },
        "notes": [
            "Billed node-hours: what the budget was charged "
            "(1-GPU ACC jobs count as a quarter node).",
            "GPU-hours = billed ACC node-hours × 4 (H100). Months use each job's start date.",
            *(
                ["Monthly charts cover members with detailed data; bsc_acct totals have no dates."]
                if project
                else ["sacct shows only your own jobs."]
            ),
        ],
    }


def render_dashboard(data: dict[str, Any]) -> str:
    # "</" is escaped so no value in the data can close the embedding <script> element.
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")

    return TEMPLATE_PATH.read_text(encoding="utf-8").replace("__DASHBOARD_DATA__", payload)


def write_dashboard(data: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_dashboard(data), encoding="utf-8")

    return path
