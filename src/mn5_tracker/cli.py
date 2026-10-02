"""`mn5track` command-line entry point."""

from dataclasses import asdict
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from mn5_tracker import scheduling
from mn5_tracker.aggregation import (
    GROUP_BY_OPTIONS,
    GROUP_KEYS,
    OCCUPANCY_CAVEAT,
    OWN_JOBS_CAVEAT,
    JobFilter,
    aggregate,
    job_family,
    load_attributed_jobs,
    project_lifetime,
    top_jobs,
    total,
)
from mn5_tracker.alerts import evaluate_alerts
from mn5_tracker.allocation_status import AllocationStatus, compute_statuses
from mn5_tracker.attribution import explain_attribution
from mn5_tracker.config import Config, find_db_path, load_config
from mn5_tracker.contributions import (
    build_contribution,
    default_export_path,
    import_contribution,
    parse_contribution,
    write_contribution,
)
from mn5_tracker.energy import (
    EnergyEstimate,
    assumptions,
    build_power_model,
    estimate,
    inputs_from_jobs,
)
from mn5_tracker.remote import RemoteRunner
from mn5_tracker.reports import charts
from mn5_tracker.reports.anonymise import anonymise_dashboard
from mn5_tracker.reports.dashboard import build_dashboard_data, write_dashboard
from mn5_tracker.reports.markdown import build_statement
from mn5_tracker.reports.summary import REPORT_COLUMNS, UNITS, Column, emit, to_json_text
from mn5_tracker.store import Store, utc_now
from mn5_tracker.sync import run_reparse, run_sync


app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Record, attribute and report compute use on MareNostrum 5. Read-only on MN5.",
)
console = Console()


class OutputFormat(StrEnum):
    table = "table"
    md = "md"
    csv = "csv"
    json = "json"


class ChartKind(StrEnum):
    monthly = "monthly"
    burndown = "burndown"


class ScheduleAction(StrEnum):
    install = "install"
    uninstall = "uninstall"
    show = "show"


FormatOption = Annotated[OutputFormat, typer.Option("--format", "-f", help="Output format.")]
DateOption = Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"])]


def open_context() -> tuple[Config, Store]:
    return load_config(), Store(find_db_path())


def as_date(value: datetime | None) -> date | None:
    return value.date() if value else None


def envelope(schema: str, **content: Any) -> dict[str, Any]:
    return {"schema": f"mn5track.{schema}/v1", "generated_at": utc_now(), **content}


@app.command()
def sync(
    since: DateOption = None,
    full: Annotated[bool, typer.Option(help="Re-read the whole history.")] = False,
    output_format: FormatOption = OutputFormat.table,
) -> None:
    """Pull sacct incrementally and snapshot bsc_acct, associations, groups and quota."""
    config, store = open_context()
    summary = run_sync(config, store, RemoteRunner(config.hosts), as_date(since), full)
    result = summary.result

    if output_format == OutputFormat.json:
        print(to_json_text(summary.to_json()))
        raise typer.Exit(1 if result.errors else 0)

    console.print(f"Host: [bold]{summary.host or 'none reachable'}[/bold]  since {summary.since}")

    for attempt in summary.failed_attempts:
        console.print(f"[yellow]fallback:[/yellow] {attempt}")

    console.print(
        f"Jobs seen {result.jobs_seen}, new {result.jobs_new}, updated {result.jobs_updated}; "
        f"budget rows {result.budget_rows}, user rows {result.user_rows}, "
        f"associations {result.association_rows}, quota {result.quota_rows}"
    )

    for change in result.visibility_changes:
        console.print(
            f"[red]visibility[/red] {change.account} {change.source}: "
            f"{change.before} → {change.after}"
        )

    for warning in result.warnings:
        console.print(f"[yellow]warning:[/yellow] {warning}")

    for error in result.errors:
        console.print(f"[red]error:[/red] {error}")

    raise typer.Exit(1 if result.errors else 0)


STATUS_COLUMNS = (
    Column("account", "Account"),
    Column("machine", "Machine"),
    Column("total_node_hours", "Budget node-h", "float0"),
    Column("used_node_hours", "Used node-h", "float0"),
    Column("used_pct", "Used", "pct100"),
    Column("expiration_date", "Expires"),
    Column("days_to_expiry", "Days left", "int"),
    Column("projected_exhaustion", "Runs out"),
    Column("visible_in_bsc_acct", "Visible", "bool"),
    Column("in_unix_group", "In group", "bool"),
    Column("snapshot_date", "Snapshot"),
)


def status_row(status: AllocationStatus) -> dict[str, Any]:
    return {**asdict(status), "snapshot_date": status.snapshot_ts[:10]}


@app.command()
def status(output_format: FormatOption = OutputFormat.table) -> None:
    """Budget, usage, expiry, burn-rate projection and visibility per allocation."""
    config, store = open_context()
    statuses = compute_statuses(config, store, date.today())
    rows = [status_row(status) for status in statuses]
    payload = envelope(
        "status",
        allocations=rows,
        units={"node_hours": "bsc_acct khours × 1000 / physical cores per node, all users"},
    )
    emit(
        console,
        output_format,
        "Allocations (bsc_acct, all users)",
        STATUS_COLUMNS,
        rows,
        payload,
        notes=["Rows with Visible = no show the final snapshot taken before access was lost."],
    )


def build_filter(
    project: str | None,
    account: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
    partition: str | None,
) -> JobFilter:
    return JobFilter(
        project=project,
        account=account,
        date_from=as_date(date_from),
        date_to=as_date(date_to),
        partition_class=partition,
    )


@app.command()
def report(
    project: Annotated[
        str | None, typer.Option(help="Scientific project, or 'unattributed'.")
    ] = None,
    account: Annotated[str | None, typer.Option(help="Slurm account (allocation).")] = None,
    date_from: Annotated[datetime | None, typer.Option("--from", formats=["%Y-%m-%d"])] = None,
    date_to: Annotated[datetime | None, typer.Option("--to", formats=["%Y-%m-%d"])] = None,
    partition: Annotated[str | None, typer.Option(help="Partition class: acc or gpp.")] = None,
    by: Annotated[str | None, typer.Option(help=f"One of: {', '.join(GROUP_BY_OPTIONS)}.")] = None,
    output_format: FormatOption = OutputFormat.table,
) -> None:
    """Aggregated usage of own jobs, optionally grouped."""
    config, store = open_context()
    job_filter = build_filter(project, account, date_from, date_to, partition)
    jobs = [job for job in load_attributed_jobs(config, store) if job_filter.matches(job)]
    rows = [row.to_json() for row in aggregate(jobs, by)] if by else []
    totals = total(jobs).to_json()
    payload = envelope(
        "report",
        filters=asdict(job_filter),
        group_by=by,
        units=UNITS,
        rows=rows,
        total=totals,
        caveats=[OWN_JOBS_CAVEAT, OCCUPANCY_CAVEAT],
    )
    emit(
        console,
        output_format,
        f"Usage by {by}" if by else "Usage",
        REPORT_COLUMNS,
        [*rows, {**totals, "key": "TOTAL"}],
        payload,
        notes=[OWN_JOBS_CAVEAT, OCCUPANCY_CAVEAT],
    )


ENERGY_COLUMNS = (
    Column("key", "Group"),
    Column("billed_node_hours", "Billed node-h", "float0"),
    Column("measured_pct", "Measured", "pct"),
    Column("facility_mwh", "Energy MWh", "float1"),
    Column("facility_mwh_range", "Energy range"),
    Column("co2e_t", "t CO2e", "float2"),
    Column("co2e_t_range", "CO2e range"),
)
ENERGY_CAVEATS = [
    "Energy = measured IPMI (exclusive GPP jobs) + modelled power × billed node-h, × PUE.",
    "ACC power is modelled (MN5 records no GPU energy): low / central / high GPU utilisation.",
    "CO2e is location-based (grid average); see `assumptions` for every parameter and source.",
]


def energy_row(key: str, result: EnergyEstimate) -> dict[str, Any]:
    facility = result.facility_kwh
    co2e = result.co2e_kg

    return {
        "key": key,
        **result.to_json(),
        "measured_pct": result.measured_share,
        "facility_mwh": facility["central"] / 1000,
        "facility_mwh_range": f"{facility['low'] / 1000:,.1f}–{facility['high'] / 1000:,.1f}",
        "co2e_t": co2e["central"] / 1000,
        "co2e_t_range": f"{co2e['low'] / 1000:,.2f}–{co2e['high'] / 1000:,.2f}",
    }


@app.command()
def energy(
    project: Annotated[str | None, typer.Option(help="Scientific project.")] = None,
    account: Annotated[str | None, typer.Option(help="Slurm account (allocation).")] = None,
    date_from: Annotated[datetime | None, typer.Option("--from", formats=["%Y-%m-%d"])] = None,
    date_to: Annotated[datetime | None, typer.Option("--to", formats=["%Y-%m-%d"])] = None,
    partition: Annotated[str | None, typer.Option(help="Partition class: acc or gpp.")] = None,
    by: Annotated[str | None, typer.Option(help=f"One of: {', '.join(GROUP_BY_OPTIONS)}.")] = None,
    output_format: FormatOption = OutputFormat.table,
) -> None:
    """Estimated energy (MWh) and carbon footprint (t CO2e) of own jobs, with ranges."""
    config, store = open_context()
    all_jobs = load_attributed_jobs(config, store)
    job_filter = build_filter(project, account, date_from, date_to, partition)
    jobs = [job for job in all_jobs if job_filter.matches(job)]
    power = build_power_model(config, inputs_from_jobs(all_jobs, config))
    groups: dict[str, list] = {}

    if by:
        if by not in GROUP_KEYS:
            raise typer.BadParameter(f"--by must be one of {', '.join(GROUP_BY_OPTIONS)}")

        for job in jobs:
            groups.setdefault(GROUP_KEYS[by](job), []).append(job)

    rows = [
        energy_row(key, estimate(inputs_from_jobs(group, config), config.energy, power))
        for key, group in sorted(groups.items())
    ]
    totals = energy_row("TOTAL", estimate(inputs_from_jobs(jobs, config), config.energy, power))
    notes = [*ENERGY_CAVEATS, OWN_JOBS_CAVEAT]

    if project:
        notes.append(
            f"Project-wide total incl. other members: `mn5track project-lifetime {project}`."
        )

    if not config.energy.verified:
        notes.insert(0, "Energy parameters are NOT verified (config/project.yaml energy.verified).")

    payload = envelope(
        "energy",
        filters=asdict(job_filter),
        group_by=by,
        rows=rows,
        total=totals,
        assumptions=assumptions(config, power),
        caveats=notes,
    )
    emit(
        console,
        output_format,
        f"Energy by {by}" if by else "Energy",
        ENERGY_COLUMNS,
        [*rows, totals],
        payload,
        notes,
    )


LIFETIME_COLUMNS = (
    Column("account", "Allocation"),
    Column("own_jobs", "Own jobs", "int"),
    Column("own_acc", "Own ACC node-h", "float0"),
    Column("own_gpp", "Own GPP node-h", "float0"),
    Column("contributed_acc", "Contributed ACC node-h", "float0"),
    Column("contributors_count", "Contributors", "int"),
    Column("fallback_acc", "bsc_acct others ACC node-h", "float0"),
    Column("total_acc", "Total ACC node-h", "float0"),
    Column("others_source", "Others source"),
)


@app.command("project-lifetime")
def project_lifetime_command(
    project: str, output_format: FormatOption = OutputFormat.table
) -> None:
    """Lifetime total of a project across all allocations (lower bound)."""
    config, store = open_context()
    lifetime = project_lifetime(config, store, load_attributed_jobs(config, store), project)
    payload = {"generated_at": utc_now(), **lifetime.to_json()}
    rows = [
        {
            **allocation.to_json(),
            "own_acc": allocation.own_billed_node_hours_by_class.get("acc"),
            "own_gpp": allocation.own_billed_node_hours_by_class.get("gpp"),
            "contributed_acc": allocation.contributed_billed_node_hours_by_class.get("acc"),
            "contributors_count": len(allocation.contributors),
            "fallback_acc": (allocation.bsc_acct_others_billed_node_hours_by_class or {}).get(
                "acc"
            ),
            "total_acc": allocation.total_billed_node_hours_by_class.get("acc"),
        }
        for allocation in lifetime.allocations
    ]
    lower_bound = lifetime.lower_bound_billed_node_hours_by_class
    notes = [
        f"Lower-bound total: {lower_bound.get('acc', 0):,.0f} ACC node-h "
        f"(≈ {lifetime.gpu_hours(lower_bound):,.0f} GPU-h billed), "
        f"{lower_bound.get('gpp', 0):,.0f} GPP node-h; own jobs {lifetime.own.jobs:,}.",
        f"Coverage: detailed data from {lifetime.members_with_detail} member(s) "
        f"({', '.join(['you', *(c.contributor for c in lifetime.contributors)])}); "
        f"bsc_acct totals for up to {lifetime.members_from_bsc_acct_only} others.",
        *[
            f"Contributor {c.contributor} used different attribution rules; ask for a re-export."
            for c in lifetime.contributors
            if not c.same_rules
        ],
        *[f"Invisible: {item['account']} — {item['reason']}" for item in lifetime.invisible],
        *lifetime.caveats,
    ]
    emit(
        console, output_format, f"{lifetime.title} lifetime", LIFETIME_COLUMNS, rows, payload, notes
    )


TOP_JOB_COLUMNS = (
    Column("job_id", "JobID"),
    Column("job_name", "Name"),
    Column("account", "Account"),
    Column("project", "Project"),
    Column("state", "State"),
    Column("start", "Start"),
    Column("billed_node_hours", "Node-h (billed)", "float0"),
    Column("gpu_hours_billed", "GPU-h (billed)", "float0"),
)


@app.command()
def top(
    n: Annotated[int, typer.Option("--n", help="How many rows.")] = 20,
    families: Annotated[bool, typer.Option(help="Group by job-name family instead.")] = False,
    project: str | None = None,
    account: str | None = None,
    date_from: Annotated[datetime | None, typer.Option("--from", formats=["%Y-%m-%d"])] = None,
    date_to: Annotated[datetime | None, typer.Option("--to", formats=["%Y-%m-%d"])] = None,
    output_format: FormatOption = OutputFormat.table,
) -> None:
    """Most expensive jobs, or job-name families, by billed node-hours."""
    config, store = open_context()
    job_filter = build_filter(project, account, date_from, date_to, None)
    jobs = [job for job in load_attributed_jobs(config, store) if job_filter.matches(job)]

    if families:
        rows = [row.to_json() for row in aggregate(jobs, "jobname-prefix")[:n]]
        columns = REPORT_COLUMNS
    else:
        rows = [asdict(job) for job in top_jobs(jobs, n)]
        columns = TOP_JOB_COLUMNS

    payload = envelope(
        "top", filters=asdict(job_filter), families=families, rows=rows, caveats=[OWN_JOBS_CAVEAT]
    )
    emit(
        console,
        output_format,
        "Top job families" if families else "Top jobs",
        columns,
        rows,
        payload,
    )


@app.command()
def explain(job_id: str, output_format: FormatOption = OutputFormat.table) -> None:
    """Show every stored field of a job and which attribution rule matched."""
    config, store = open_context()
    job = store.get_job(job_id)

    if job is None:
        console.print(f"[red]Job {job_id} is not in the store; run `mn5track sync`.[/red]")
        raise typer.Exit(1)

    evaluations = explain_attribution(job, config.attribution_rules)
    matched = next((evaluation for evaluation in evaluations if evaluation.matched), None)
    payload = envelope(
        "explain",
        job={**asdict(job), "job_family": job_family(job.job_name)},
        attribution={
            "project": matched.rule.project if matched else "unattributed",
            "rule": matched.rule.describe() if matched else None,
            "evaluated": [
                {
                    "rule": evaluation.rule.describe(),
                    "matched": evaluation.matched,
                    "failed_conditions": list(evaluation.failed_conditions),
                }
                for evaluation in evaluations
            ],
        },
    )

    if output_format == OutputFormat.json:
        print(to_json_text(payload))
        return

    rows = [
        {"field": key, "value": round(value, 3) if isinstance(value, float) else value}
        for key, value in payload["job"].items()
    ]
    emit(
        console,
        output_format,
        f"Job {job_id}",
        (Column("field", "Field"), Column("value", "Value")),
        rows,
        payload,
    )
    console.print(f"Project: [bold]{payload['attribution']['project']}[/bold]")

    for evaluation in payload["attribution"]["evaluated"]:
        verdict = "[green]match[/green]" if evaluation["matched"] else "[dim]no[/dim]"
        failures = ", ".join(evaluation["failed_conditions"])
        console.print(
            f"  {verdict} {evaluation['rule']}" + (f"  (failed: {failures})" if failures else "")
        )


@app.command()
def statement(
    project: str,
    output_format: Annotated[OutputFormat, typer.Option("--format", "-f")] = OutputFormat.md,
) -> None:
    """Markdown paragraph + table for a paper's acknowledgements or a proposal."""
    config, store = open_context()
    lifetime = project_lifetime(config, store, load_attributed_jobs(config, store), project)
    text = build_statement(config, lifetime, date.today())

    if output_format == OutputFormat.json:
        print(
            to_json_text(
                {
                    **envelope("statement", project=project),
                    "markdown": text,
                    "lifetime": lifetime.to_json(),
                }
            )
        )
        return

    print(text, end="")


ALERT_COLUMNS = (
    Column("severity", "Severity"),
    Column("kind", "Kind"),
    Column("account", "Account"),
    Column("message", "Message"),
)


@app.command()
def alerts(output_format: FormatOption = OutputFormat.table) -> None:
    """List budget, expiry, exhaustion, visibility and stale-data alerts; exit 1 if any."""
    config, store = open_context()
    today = date.today()
    found = evaluate_alerts(config, store, compute_statuses(config, store, today), today)
    rows = [asdict(alert) for alert in found]

    if not found and output_format != OutputFormat.json:
        console.print("[green]No alerts.[/green]")
    else:
        emit(console, output_format, "Alerts", ALERT_COLUMNS, rows, envelope("alerts", alerts=rows))

    raise typer.Exit(1 if found else 0)


@app.command()
def chart(
    kind: ChartKind,
    out_dir: Annotated[Path, typer.Option("--out", help="Output directory.")] = Path("charts"),
    metric: Annotated[
        str, typer.Option(help="monthly: billed_node_hours, gpu_hours_billed, node_hours, khours.")
    ] = "billed_node_hours",
    project: str | None = None,
) -> None:
    """Write PNG + self-contained HTML charts."""
    config, store = open_context()
    jobs = [
        job
        for job in load_attributed_jobs(config, store)
        if JobFilter(project=project).matches(job)
    ]

    if kind == ChartKind.monthly:
        figure, rows = charts.monthly_burn_figure(config, jobs, metric)
        title = "Monthly compute by allocation"
    else:
        statuses = [
            status
            for status in compute_statuses(config, store, date.today())
            if status.visible_in_bsc_acct
        ]
        figure, rows = charts.burndown_figure(config, store, jobs, statuses)
        title = "Allocation burn-down"

    for path in charts.write_chart(figure, rows, out_dir, f"{kind.value}", title):
        console.print(f"wrote {path}")


@app.command()
def export(
    project: str,
    out: Annotated[
        Path | None, typer.Option(help="Default: contributions/<project>/<user>.json")
    ] = None,
) -> None:
    """Write your own usage for a project as an aggregated file other members can import."""
    config, store = open_context()
    contribution = build_contribution(config, store, load_attributed_jobs(config, store), project)

    if not contribution.rows:
        console.print(f"[yellow]No jobs attributed to {project!r}; nothing to export.[/yellow]")
        raise typer.Exit(1)

    path = write_contribution(contribution, out or default_export_path(contribution))
    console.print(
        f"Wrote {path}: {contribution.jobs:,} jobs in {len(contribution.rows)} aggregated rows "
        f"(no job names, paths or IDs). Share it with the project; it contains your username."
    )


@app.command("import")
def import_command(
    files: Annotated[list[Path], typer.Argument(help="Contribution files or directories.")],
    output_format: FormatOption = OutputFormat.table,
) -> None:
    """Import other members' contribution files (newest per member and project wins)."""
    config, store = open_context()
    paths = [
        candidate
        for path in files
        for candidate in (sorted(path.rglob("*.json")) if path.is_dir() else [path])
    ]
    outcomes = [asdict(import_contribution(config, store, path)) for path in paths]
    rows = [{**outcome, "warnings": "; ".join(outcome["warnings"])} for outcome in outcomes]
    columns = (
        Column("source", "File"),
        Column("contributor", "Member"),
        Column("project", "Project"),
        Column("status", "Status"),
        Column("warnings", "Warnings"),
    )
    emit(
        console,
        output_format,
        "Imported contributions",
        columns,
        rows,
        envelope("import", results=outcomes),
    )

    raise typer.Exit(1 if any(outcome["status"] == "invalid" for outcome in outcomes) else 0)


@app.command()
def contributors(
    project: str | None = None, output_format: FormatOption = OutputFormat.table
) -> None:
    """List imported contributions: who, which project, how fresh, same rules or not."""
    config, store = open_context()
    rows = []

    for payload in store.contribution_payloads(project):
        contribution = parse_contribution(payload)
        billed = contribution.billed_node_hours_by_class()
        rows.append(
            {
                "contributor": contribution.contributor,
                "project": contribution.project,
                "generated_at": contribution.generated_at,
                "last_sync": contribution.last_sync,
                "jobs": contribution.jobs,
                "acc_billed_node_hours": billed.get("acc", 0.0),
                "gpp_billed_node_hours": billed.get("gpp", 0.0),
                "same_rules": contribution.rules_fingerprint == config.rules_fingerprint,
            }
        )

    columns = (
        Column("contributor", "Member"),
        Column("project", "Project"),
        Column("generated_at", "Exported"),
        Column("jobs", "Jobs", "int"),
        Column("acc_billed_node_hours", "ACC node-h", "float0"),
        Column("gpp_billed_node_hours", "GPP node-h", "float0"),
        Column("same_rules", "Same rules", "bool"),
    )
    emit(
        console,
        output_format,
        "Contributions",
        columns,
        rows,
        envelope("contributors", contributors=rows),
    )


@app.command()
def dashboard(
    project: Annotated[str | None, typer.Option(help="Project view, incl. contributions.")] = None,
    all_views: Annotated[
        bool,
        typer.Option("--all", help="Own view plus every project in `dashboards.projects`."),
    ] = False,
    out: Annotated[
        Path | None,
        typer.Option(
            help="File, or folder with --all. Default: `dashboards.out_dir`, else next to the DB."
        ),
    ] = None,
    anonymise: Annotated[
        bool,
        typer.Option(
            "--anonymise",
            help="Replace project, allocation and user names with aliases; hours stay real.",
        ),
    ] = False,
    output_format: Annotated[
        OutputFormat, typer.Option("--format", "-f", help="table or json.")
    ] = OutputFormat.table,
) -> None:
    """Write a self-contained HTML page with overall usage, outcomes and allocation burn-down."""
    config, store = open_context()
    views = [None, *config.dashboards.projects] if all_views else [project]

    def view_data(view: str | None) -> dict[str, Any]:
        data = build_dashboard_data(config, store, date.today(), view)

        return anonymise_dashboard(data, config) if anonymise else data

    if output_format == OutputFormat.json:
        print(to_json_text(view_data(views[0])))
        return

    out_dir = config.dashboards.out_dir or store.path.parent

    if all_views and out:
        out_dir = out

    for view in views:
        data = view_data(view)
        default_path = out_dir / dashboard_file_name(data, anonymise)
        path = write_dashboard(data, default_path if all_views else out or default_path)
        console.print(f"wrote {path}")


def dashboard_file_name(data: dict[str, Any], anonymised: bool) -> str:
    """`dashboard[-<project>][-anonymised].html`; an anonymised page is named by its alias."""
    scope = data["scope"]
    suffix = "-anonymised" if anonymised else ""

    if scope["kind"] != "project":
        return f"dashboard{suffix}.html"

    project = scope["title"] if anonymised else scope["project"]

    return f"dashboard-{project.lower().replace(' ', '-')}{suffix}.html"


@app.command()
def reparse(output_format: FormatOption = OutputFormat.table) -> None:
    """Rebuild jobs and snapshots from the stored raw outputs."""
    config, store = open_context()
    results = run_reparse(config, store)
    errors = [f"{result.snapshot_ts}: {error}" for result in results for error in result.errors]
    payload = envelope("reparse", runs=len(results), jobs=len(store.load_jobs()), errors=errors)

    if output_format == OutputFormat.json:
        print(to_json_text(payload))
    else:
        console.print(f"Replayed {payload['runs']} raw runs; {payload['jobs']} jobs in store.")

        for error in errors:
            console.print(f"[red]error:[/red] {error}")

    raise typer.Exit(1 if errors else 0)


@app.command()
def schedule(
    action: ScheduleAction,
    hour: Annotated[int, typer.Option(help="Local hour for the daily sync.")] = 7,
) -> None:
    """Install/uninstall/show a macOS launchd job that runs `mn5track sync` daily."""
    if action == ScheduleAction.show:
        print(scheduling.render_plist(hour), end="")
        return

    if action == ScheduleAction.install:
        path = scheduling.install(hour)
        console.print(f"Installed {path}; daily sync at {hour:02d}:00. Logs: {scheduling.LOG_PATH}")
        return

    scheduling.uninstall()
    console.print("Removed the launchd job.")
