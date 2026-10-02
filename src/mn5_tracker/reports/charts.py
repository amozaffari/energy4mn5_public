"""Static charts: monthly burn stacked by account, and burn-down per allocation vs budget."""

import base64
import html
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from matplotlib.axes import Axes
from matplotlib.dates import AutoDateLocator, ConciseDateFormatter, date2num
from matplotlib.figure import Figure

from mn5_tracker.allocation_status import AllocationStatus
from mn5_tracker.config import Config
from mn5_tracker.metrics import khours_to_node_hours
from mn5_tracker.store import JobRecord, Store


# Validated categorical order (dataviz reference palette, light mode); assigned by account order
# in tracker.yaml so an account keeps its colour across charts and filters.
SERIES_COLORS = (
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#4a3aa7",
    "#e34948",
)
OTHER_COLOR = "#8a8984"
SURFACE = "#fcfcfb"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID = "#e4e3df"
MAX_SERIES = len(SERIES_COLORS)
METRIC_LABELS = {
    "billed_node_hours": "Billed node-hours",
    "gpu_hours_billed": "Billed GPU-hours",
    "node_hours": "Occupied node-hours",
    "khours": "khours (bsc_acct units)",
}


def style_axes(axes: Axes) -> None:
    axes.set_facecolor(SURFACE)
    axes.grid(axis="y", color=GRID, linewidth=0.8)
    axes.set_axisbelow(True)

    for side in ("top", "right", "left"):
        axes.spines[side].set_visible(False)

    axes.spines["bottom"].set_color(GRID)
    axes.tick_params(colors=TEXT_SECONDARY, labelsize=9, length=0)


def account_colors(config: Config, accounts: list[str]) -> dict[str, str]:
    ordered = [account for account in config.allocations if account in accounts]
    ordered += sorted(account for account in accounts if account not in ordered)

    return {
        account: SERIES_COLORS[index] if index < MAX_SERIES else OTHER_COLOR
        for index, account in enumerate(ordered)
    }


def job_metric(job: JobRecord, metric: str) -> float:
    if metric == "khours":
        return job.core_hours_billed / 1000

    return float(getattr(job, metric))


def monthly_series(jobs: list[JobRecord], metric: str) -> dict[str, dict[str, float]]:
    """{month: {account: value}} using each job's start month."""
    series: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    for job in jobs:
        if job.start_date is None:
            continue

        series[job.start_date.strftime("%Y-%m")][job.account] += job_metric(job, metric)

    return series


def monthly_burn_figure(
    config: Config, jobs: list[JobRecord], metric: str
) -> tuple[Figure, list[dict[str, Any]]]:
    series = monthly_series(jobs, metric)
    months = sorted(series)
    accounts = sorted({account for values in series.values() for account in values})
    colors = account_colors(config, accounts)
    ordered_accounts = sorted(accounts, key=list(colors).index)

    figure = Figure(figsize=(11, 5), facecolor=SURFACE)
    axes = figure.subplots()
    style_axes(axes)
    bottoms = [0.0] * len(months)

    for account in ordered_accounts:
        values = [series[month].get(account, 0.0) for month in months]
        axes.bar(
            months,
            values,
            bottom=bottoms,
            color=colors[account],
            edgecolor=SURFACE,
            linewidth=2,
            width=0.8,
            label=account,
        )
        bottoms = [bottom + value for bottom, value in zip(bottoms, values, strict=True)]

    axes.set_ylabel(METRIC_LABELS.get(metric, metric), color=TEXT_SECONDARY)
    axes.set_title(
        f"Monthly compute by allocation — {METRIC_LABELS.get(metric, metric)} (own jobs)",
        color=TEXT_PRIMARY,
        loc="left",
        fontsize=12,
    )
    axes.tick_params(axis="x", rotation=60)
    axes.legend(frameon=False, ncols=min(len(ordered_accounts), 5), loc="upper left", fontsize=9)
    figure.tight_layout()

    rows = [
        {
            "month": month,
            **{account: round(series[month].get(account, 0.0), 1) for account in ordered_accounts},
        }
        for month in months
    ]

    return figure, rows


def burndown_figure(
    config: Config, store: Store, jobs: list[JobRecord], statuses: list[AllocationStatus]
) -> tuple[Figure, list[dict[str, Any]]]:
    """Small multiples: one axis per budgeted allocation, cumulative node-h vs the budget line."""
    budgeted = [status for status in statuses if status.total_khours]

    if not budgeted:
        raise ValueError(
            "No budgeted allocation in the bsc_acct snapshots yet; run `mn5track sync`"
        )

    columns = min(len(budgeted), 3)
    rows_count = -(-len(budgeted) // columns)
    figure = Figure(figsize=(5 * columns, 3.6 * rows_count), facecolor=SURFACE)
    axes_grid = figure.subplots(rows_count, columns, squeeze=False)
    colors = account_colors(config, [status.account for status in budgeted])
    table_rows: list[dict[str, Any]] = []

    for axes, status in zip(axes_grid.flat, budgeted, strict=False):
        plot_burndown(axes, config, store, jobs, status, colors[status.account])
        table_rows.append(
            {
                "account": status.account,
                "budget_node_hours": round(status.total_node_hours or 0, 0),
                "used_node_hours": round(status.used_node_hours, 0),
                "used_pct": round(status.used_pct or 0, 1),
                "expires": status.expiration_date.isoformat() if status.expiration_date else "",
                "projected_exhaustion": (
                    status.projected_exhaustion.isoformat() if status.projected_exhaustion else ""
                ),
            }
        )

    for axes in list(axes_grid.flat)[len(budgeted) :]:
        axes.set_visible(False)

    figure.suptitle(
        "Allocation burn-down (billed node-hours)", color=TEXT_PRIMARY, x=0.01, ha="left"
    )
    handles, labels = axes_grid.flat[0].get_legend_handles_labels()
    figure.legend(handles, labels, loc="lower center", ncols=3, frameon=False, fontsize=9)
    figure.tight_layout(rect=(0, 0.06, 1, 1))

    return figure, table_rows


def plot_burndown(
    axes: Axes,
    config: Config,
    store: Store,
    jobs: list[JobRecord],
    status: AllocationStatus,
    color: str,
) -> None:
    node_type = config.node_types[status.node_class]
    own_jobs = sorted(
        (
            job
            for job in jobs
            if job.account == status.account
            and job.partition_class == status.node_class
            and job.end
        ),
        key=lambda job: job.end or "",
    )
    cumulative = 0.0
    own_dates = []
    own_values = []

    for job in own_jobs:
        cumulative += job.billed_node_hours
        own_dates.append(datetime.fromisoformat(job.end or ""))
        own_values.append(cumulative)

    snapshots = [
        row for row in store.budget_snapshots(status.account) if row["machine"] == status.machine
    ]
    snapshot_dates = [
        datetime.fromisoformat(row["snapshot_ts"]).replace(tzinfo=None) for row in snapshots
    ]
    snapshot_values = [khours_to_node_hours(row["used_khours"], node_type) for row in snapshots]

    style_axes(axes)
    axes.step(
        date2num(own_dates),
        own_values,
        where="post",
        color=color,
        linewidth=2,
        label="own jobs (sacct)",
    )
    axes.plot(
        date2num(snapshot_dates),
        snapshot_values,
        linestyle="none",
        marker="o",
        markersize=8,
        markerfacecolor=color,
        markeredgecolor=SURFACE,
        markeredgewidth=2,
        label="all users (bsc_acct)",
    )
    axes.axhline(
        status.total_node_hours or 0,
        color=TEXT_SECONDARY,
        linestyle="--",
        linewidth=1.5,
        label="budget",
    )

    if status.expiration_date:
        expiry_x = float(date2num(datetime.combine(status.expiration_date, datetime.min.time())))
        axes.axvline(expiry_x, color=GRID, linewidth=1.5)
        axes.annotate(
            f"expires {status.expiration_date}",
            (
                expiry_x,
                status.total_node_hours or 0,
            ),
            textcoords="offset points",
            xytext=(-4, 6),
            ha="right",
            fontsize=8,
            color=TEXT_SECONDARY,
        )

    axes.set_title(
        f"{status.account} — {status.used_pct or 0:.0f}% of "
        f"{status.total_node_hours or 0:,.0f} node-h",
        color=TEXT_PRIMARY,
        loc="left",
        fontsize=10,
    )
    span_start = min(
        [*own_dates, *snapshot_dates, datetime.now() - timedelta(days=30)], default=datetime.now()
    )
    allocation = config.allocations.get(status.account)

    if allocation and allocation.start:
        span_start = min(span_start, datetime.combine(allocation.start, datetime.min.time()))

    span_end = max(
        datetime.combine(status.expiration_date, datetime.min.time())
        if status.expiration_date
        else datetime.now(),
        datetime.now(),
    )
    margin = (span_end - span_start) * 0.04
    axes.set_xlim(float(date2num(span_start - margin)), float(date2num(span_end + margin)))
    axes.set_ylim(0, (status.total_node_hours or max(own_values, default=1.0)) * 1.12)
    locator = AutoDateLocator(maxticks=6)
    axes.xaxis.set_major_locator(locator)
    axes.xaxis.set_major_formatter(ConciseDateFormatter(locator))


def figure_to_png(figure: Figure, path: Path) -> bytes:
    figure.savefig(path, dpi=150, facecolor=SURFACE)

    return path.read_bytes()


def write_html(path: Path, title: str, png: bytes, rows: list[dict[str, Any]]) -> None:
    """Self-contained page: the PNG plus the same numbers as a table (the accessible view)."""
    headers = list(rows[0]) if rows else []
    header_html = "".join(f"<th>{html.escape(str(header))}</th>" for header in headers)
    body_html = "".join(
        "<tr>"
        + "".join(f"<td>{html.escape(str(row[header]))}</td>" for header in headers)
        + "</tr>"
        for row in rows
    )
    encoded = base64.b64encode(png).decode("ascii")
    path.write_text(
        f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{html.escape(title)}</title>
<style>
body {{
  background: {SURFACE}; color: {TEXT_PRIMARY}; font: 14px system-ui, sans-serif; margin: 16px;
}}
img {{ max-width: 100%; height: auto; }}
table {{ border-collapse: collapse; margin-top: 16px; font-variant-numeric: tabular-nums; }}
th, td {{ padding: 4px 10px; border-bottom: 1px solid {GRID}; text-align: right; }}
th:first-child, td:first-child {{ text-align: left; }}
.wrap {{ overflow-x: auto; }}
</style></head><body>
<h1>{html.escape(title)}</h1>
<img alt="{html.escape(title)}" src="data:image/png;base64,{encoded}">
<div class="wrap"><table>
<thead><tr>{header_html}</tr></thead><tbody>{body_html}</tbody>
</table></div>
<p style="color:{TEXT_SECONDARY}">Generated by mn5track on {date.today().isoformat()}.</p>
</body></html>
"""
    )


def write_chart(
    figure: Figure, rows: list[dict[str, Any]], out_dir: Path, name: str, title: str
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    png_path = out_dir / f"{name}.png"
    html_path = out_dir / f"{name}.html"
    write_html(html_path, title, figure_to_png(figure, png_path), rows)

    return [png_path, html_path]
