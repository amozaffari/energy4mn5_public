"""Render tabular results as rich tables, Markdown, CSV or JSON (stable, versioned schema)."""

import csv
import io
import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from typing import Any

from rich.console import Console
from rich.table import Table


@dataclass(frozen=True)
class Column:
    key: str
    header: str
    kind: str = "text"


REPORT_COLUMNS = (
    Column("key", "Group"),
    Column("jobs", "Jobs", "int"),
    Column("production_success_rate", "OK % (prod)", "pct"),
    Column("node_hours", "Node-h (occupied)", "float0"),
    Column("billed_node_hours", "Node-h (billed)", "float0"),
    Column("gpu_hours_billed", "GPU-h (billed)", "float0"),
    Column("gpu_hours_node", "GPU-h (full node)", "float0"),
    Column("gpu_hours_requested", "GPU-h (requested)", "float0"),
    Column("khours_billed", "khours", "float1"),
    Column("wasted_billed_node_hours", "Wasted node-h", "float0"),
    Column("debug_failed_billed_node_hours", "Debug-failed node-h", "float0"),
    Column("timeout_billed_node_hours", "Timeout node-h", "float0"),
)

UNITS = {
    "node_hours": "nodes occupied × wall-clock hours (ElapsedRaw × NNodes)",
    "billed_node_hours": "billing TRES × hours / hw_threads per node (what the budget is charged)",
    "gpu_hours_billed": "billed_node_hours × GPUs per node (H100 on ACC); headline GPU number",
    "gpu_hours_node": "node_hours × GPUs per node (full-node view)",
    "gpu_hours_requested": "gres/gpu × hours",
    "khours_billed": "thousand physical-core-hours, as in bsc_acct",
    "wasted_billed_node_hours": "billed node-h of failed/cancelled/OOM jobs in production QOS",
    "debug_failed_billed_node_hours": "billed node-h of failed jobs in development QOS (expected)",
    "production_success_rate": "completed / finished jobs in production QOS",
    "timeout_billed_node_hours": "billed node-h of TIMEOUT jobs (often intentional, checkpointed)",
    "energy_kwh": "raw sacct ConsumedEnergyRaw (GPP only, whole node, so it overstates "
    "shared-node jobs); use `mn5track energy` for estimates",
}


def json_default(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()

    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)

    raise TypeError(f"Not JSON serialisable: {type(value).__name__}")


def to_json_text(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=json_default, ensure_ascii=False)


def format_cell(value: Any, kind: str) -> str:
    if value is None:
        return "–"

    if kind == "int":
        return f"{value:,}"

    if kind == "float0":
        return f"{value:,.0f}"

    if kind == "float1":
        return f"{value:,.1f}"

    if kind == "float2":
        return f"{value:,.2f}"

    if kind == "pct":
        return f"{value * 100:.0f}%"

    if kind == "pct100":
        return f"{value:.0f}%"

    if kind == "bool":
        return "yes" if value else "no"

    return str(value)


def render_rich(
    console: Console, title: str, columns: tuple[Column, ...], rows: list[dict[str, Any]]
) -> None:
    table = Table(title=title, title_justify="left", header_style="bold")

    for column in columns:
        table.add_column(column.header, justify="left" if column.kind == "text" else "right")

    for row in rows:
        table.add_row(*(format_cell(row.get(column.key), column.kind) for column in columns))

    console.print(table)


def render_markdown(columns: tuple[Column, ...], rows: list[dict[str, Any]]) -> str:
    header = "| " + " | ".join(column.header for column in columns) + " |"
    divider = (
        "| " + " | ".join("---" if column.kind == "text" else "---:" for column in columns) + " |"
    )
    body = [
        "| "
        + " | ".join(format_cell(row.get(column.key), column.kind) for column in columns)
        + " |"
        for row in rows
    ]

    return "\n".join([header, divider, *body])


def render_csv(columns: tuple[Column, ...], rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=[column.key for column in columns], extrasaction="ignore"
    )
    writer.writeheader()
    writer.writerows(rows)

    return buffer.getvalue()


def emit(
    console: Console,
    output_format: str,
    title: str,
    columns: tuple[Column, ...],
    rows: list[dict[str, Any]],
    payload: dict[str, Any],
    notes: list[str] | None = None,
) -> None:
    """Print rows in the requested format; `payload` is the full JSON document."""
    if output_format == "json":
        print(to_json_text(payload))
        return

    if output_format == "csv":
        print(render_csv(columns, rows), end="")
        return

    if output_format == "md":
        print(f"### {title}\n\n{render_markdown(columns, rows)}")

        for note in notes or []:
            print(f"\n> {note}")

        return

    render_rich(console, title, columns, rows)

    for note in notes or []:
        console.print(f"[dim]{note}[/dim]")
