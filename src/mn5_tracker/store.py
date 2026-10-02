"""SQLite store: one row per job, plus append-only snapshots that are never overwritten."""

import json
import sqlite3
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from mn5_tracker.attribution import Attribution
from mn5_tracker.collectors.bsc_acct import BscAcctReport, UserUsageRow
from mn5_tracker.collectors.quota import QuotaRow
from mn5_tracker.collectors.sacct import TERMINAL_STATES, SacctJob
from mn5_tracker.metrics import JobUsage


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    job_name TEXT NOT NULL,
    account TEXT NOT NULL,
    partition TEXT NOT NULL,
    qos TEXT NOT NULL,
    state TEXT NOT NULL,
    state_raw TEXT NOT NULL,
    submit TEXT,
    start TEXT,
    "end" TEXT,
    elapsed_s INTEGER NOT NULL,
    n_nodes INTEGER NOT NULL,
    alloc_cpus INTEGER NOT NULL,
    alloc_tres TEXT NOT NULL,
    cpu_time_raw_s INTEGER NOT NULL,
    exit_code TEXT NOT NULL,
    work_dir TEXT NOT NULL,
    consumed_energy_j INTEGER,
    partition_class TEXT,
    node_hours REAL NOT NULL,
    billed_node_hours REAL NOT NULL,
    gpu_hours_node REAL NOT NULL,
    gpu_hours_billed REAL NOT NULL,
    gpu_hours_requested REAL NOT NULL,
    core_hours_billed REAL NOT NULL,
    project TEXT NOT NULL,
    attribution_rule INTEGER,
    first_seen TEXT NOT NULL,
    last_updated TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_account ON jobs (account);
CREATE INDEX IF NOT EXISTS jobs_submit ON jobs (submit);

CREATE TABLE IF NOT EXISTS budget_snapshots (
    snapshot_ts TEXT NOT NULL,
    account TEXT NOT NULL,
    project_title TEXT NOT NULL,
    machine TEXT NOT NULL,
    total_khours REAL,
    used_khours REAL NOT NULL,
    used_pct REAL,
    expiration_date TEXT,
    accounting_updated TEXT
);
CREATE INDEX IF NOT EXISTS budget_snapshots_account ON budget_snapshots (account, snapshot_ts);

CREATE TABLE IF NOT EXISTS user_usage_snapshots (
    snapshot_ts TEXT NOT NULL,
    account TEXT NOT NULL,
    user TEXT NOT NULL,
    full_name TEXT NOT NULL,
    machine TEXT NOT NULL,
    used_khours REAL NOT NULL,
    pct REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS user_usage_account ON user_usage_snapshots (account, snapshot_ts);

CREATE TABLE IF NOT EXISTS associations_snapshots (
    snapshot_ts TEXT NOT NULL,
    account TEXT NOT NULL,
    partitions TEXT NOT NULL,
    qos TEXT NOT NULL,
    has_association INTEGER NOT NULL,
    in_unix_group INTEGER NOT NULL,
    in_bsc_acct INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS quota_snapshots (
    snapshot_ts TEXT NOT NULL,
    group_name TEXT NOT NULL,
    filesystem TEXT NOT NULL,
    quota_type TEXT NOT NULL,
    usage_bytes INTEGER NOT NULL,
    quota_bytes INTEGER NOT NULL,
    limit_bytes INTEGER NOT NULL,
    files INTEGER NOT NULL,
    grace TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contributions (
    contributor TEXT NOT NULL,
    project TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    source TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (contributor, project)
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    host TEXT,
    duration_s REAL NOT NULL,
    since TEXT,
    jobs_seen INTEGER NOT NULL,
    jobs_new INTEGER NOT NULL,
    jobs_updated INTEGER NOT NULL,
    budget_rows INTEGER NOT NULL,
    user_rows INTEGER NOT NULL,
    association_rows INTEGER NOT NULL,
    quota_rows INTEGER NOT NULL,
    errors TEXT NOT NULL,
    warnings TEXT NOT NULL,
    ok INTEGER NOT NULL
);
"""

SNAPSHOT_TABLES = (
    "budget_snapshots",
    "user_usage_snapshots",
    "associations_snapshots",
    "quota_snapshots",
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    job_name: str
    account: str
    partition: str
    qos: str
    state: str
    state_raw: str
    submit: str | None
    start: str | None
    end: str | None
    elapsed_s: int
    n_nodes: int
    alloc_cpus: int
    alloc_tres: str
    cpu_time_raw_s: int
    exit_code: str
    work_dir: str
    consumed_energy_j: int | None
    partition_class: str | None
    node_hours: float
    billed_node_hours: float
    gpu_hours_node: float
    gpu_hours_billed: float
    gpu_hours_requested: float
    core_hours_billed: float
    project: str
    attribution_rule: int | None
    first_seen: str
    last_updated: str
    # Derived at load time from `development_qos`, like `project`; not stored in the database.
    queue: str = field(default="production", metadata={"stored": False})

    @classmethod
    def from_parts(
        cls, job: SacctJob, usage: JobUsage, attribution: Attribution, seen_at: str
    ) -> "JobRecord":
        return cls(
            **asdict(job),
            partition_class=usage.partition_class,
            node_hours=usage.node_hours,
            billed_node_hours=usage.billed_node_hours,
            gpu_hours_node=usage.gpu_hours_node,
            gpu_hours_billed=usage.gpu_hours_billed,
            gpu_hours_requested=usage.gpu_hours_requested,
            core_hours_billed=usage.core_hours_billed,
            project=attribution.project,
            attribution_rule=attribution.rule_index,
            first_seen=seen_at,
            last_updated=seen_at,
        )

    @property
    def start_date(self) -> date | None:
        timestamp = self.start or self.submit

        return date.fromisoformat(timestamp[:10]) if timestamp else None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES


JOB_COLUMNS = tuple(
    column.name for column in fields(JobRecord) if column.metadata.get("stored", True)
)
JOB_MUTABLE_COLUMNS = tuple(
    column for column in JOB_COLUMNS if column not in ("job_id", "first_seen")
)
JOB_COLUMN_LIST = ", ".join(f'"{column}"' for column in JOB_COLUMNS)


@dataclass(frozen=True)
class UpsertCounts:
    new: int
    updated: int
    unchanged_terminal: int


@dataclass(frozen=True)
class SyncLogEntry:
    started_at: str
    finished_at: str
    kind: str
    host: str | None
    duration_s: float
    since: str | None
    jobs_seen: int
    jobs_new: int
    jobs_updated: int
    budget_rows: int
    user_rows: int
    association_rows: int
    quota_rows: int
    errors: list[str]
    warnings: list[str]
    ok: bool


@dataclass(frozen=True)
class AssociationSnapshotRow:
    snapshot_ts: str
    account: str
    partitions: str
    qos: str
    has_association: bool
    in_unix_group: bool
    in_bsc_acct: bool


class Store:
    """Thin typed wrapper over the tracker's SQLite database."""

    path: Path
    connection: sqlite3.Connection

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)

    @property
    def raw_dir(self) -> Path:
        return self.path.parent / "raw"

    def close(self) -> None:
        self.connection.close()

    def upsert_jobs(self, records: list[JobRecord]) -> UpsertCounts:
        """Insert new jobs; refresh non-terminal ones; leave terminal jobs untouched."""
        existing_states = {
            row["job_id"]: row["state"]
            for row in self.connection.execute("SELECT job_id, state FROM jobs")
        }
        new = sum(1 for record in records if record.job_id not in existing_states)
        unchanged = sum(
            1 for record in records if existing_states.get(record.job_id) in TERMINAL_STATES
        )

        placeholders = ", ".join("?" for _ in JOB_COLUMNS)
        updates = ", ".join(f'"{column}" = excluded."{column}"' for column in JOB_MUTABLE_COLUMNS)
        terminal_list = ", ".join(f"'{state}'" for state in sorted(TERMINAL_STATES))
        statement = (
            f"INSERT INTO jobs ({JOB_COLUMN_LIST}) VALUES ({placeholders}) "
            f"ON CONFLICT(job_id) DO UPDATE SET {updates} "
            f"WHERE jobs.state NOT IN ({terminal_list})"
        )

        with self.connection:
            self.connection.executemany(
                statement,
                [tuple(getattr(record, column) for column in JOB_COLUMNS) for record in records],
            )

        return UpsertCounts(
            new=new, updated=len(records) - new - unchanged, unchanged_terminal=unchanged
        )

    def update_attributions(self, attributions: dict[str, Attribution]) -> int:
        """Re-apply the current rules to stored jobs; returns how many jobs changed project."""
        current = {
            row["job_id"]: (row["project"], row["attribution_rule"])
            for row in self.connection.execute("SELECT job_id, project, attribution_rule FROM jobs")
        }
        changes = [
            (attribution.project, attribution.rule_index, job_id)
            for job_id, attribution in attributions.items()
            if current.get(job_id) != (attribution.project, attribution.rule_index)
        ]

        with self.connection:
            self.connection.executemany(
                "UPDATE jobs SET project = ?, attribution_rule = ? WHERE job_id = ?", changes
            )

        return len(changes)

    def load_jobs(self) -> list[JobRecord]:
        rows = self.connection.execute(f"SELECT {JOB_COLUMN_LIST} FROM jobs ORDER BY submit")

        return [JobRecord(**dict(row)) for row in rows]

    def get_job(self, job_id: str) -> JobRecord | None:
        row = self.connection.execute(
            f"SELECT {JOB_COLUMN_LIST} FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()

        return JobRecord(**dict(row)) if row else None

    def latest_submit(self) -> str | None:
        return self.connection.execute("SELECT MAX(submit) FROM jobs").fetchone()[0]

    def insert_bsc_acct(self, snapshot_ts: str, report: BscAcctReport) -> tuple[int, int]:
        accounting_updated = (
            report.accounting_updated.isoformat() if report.accounting_updated else None
        )
        budget_rows = [
            (
                snapshot_ts,
                budget.account,
                budget.project_title,
                budget.machine,
                budget.total_khours,
                budget.used_khours,
                budget.used_pct,
                budget.expiration_date.isoformat() if budget.expiration_date else None,
                accounting_updated,
            )
            for budget in report.budgets
        ]
        user_rows = [
            (
                snapshot_ts,
                usage.account,
                usage.user,
                usage.full_name,
                usage.machine,
                usage.used_khours,
                usage.pct,
            )
            for usage in report.user_usage
        ]

        with self.connection:
            self.connection.executemany(
                "INSERT INTO budget_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", budget_rows
            )
            self.connection.executemany(
                "INSERT INTO user_usage_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)", user_rows
            )

        return len(budget_rows), len(user_rows)

    def insert_associations(self, rows: list[AssociationSnapshotRow]) -> int:
        with self.connection:
            self.connection.executemany(
                "INSERT INTO associations_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        row.snapshot_ts,
                        row.account,
                        row.partitions,
                        row.qos,
                        int(row.has_association),
                        int(row.in_unix_group),
                        int(row.in_bsc_acct),
                    )
                    for row in rows
                ],
            )

        return len(rows)

    def insert_quota(self, snapshot_ts: str, rows: list[QuotaRow]) -> int:
        with self.connection:
            self.connection.executemany(
                "INSERT INTO quota_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        snapshot_ts,
                        row.group,
                        row.filesystem,
                        row.quota_type,
                        row.usage_bytes,
                        row.quota_bytes,
                        row.limit_bytes,
                        row.files,
                        row.grace,
                    )
                    for row in rows
                ],
            )

        return len(rows)

    def budget_snapshots(self, account: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM budget_snapshots"
        parameters: tuple[str, ...] = ()

        if account:
            query += " WHERE account = ?"
            parameters = (account,)

        rows = self.connection.execute(query + " ORDER BY snapshot_ts", parameters)

        return [dict(row) for row in rows]

    def snapshot_timestamps(self, table: str) -> list[str]:
        if table not in SNAPSHOT_TABLES:
            raise ValueError(f"Unknown snapshot table: {table}")

        rows = self.connection.execute(f"SELECT DISTINCT snapshot_ts FROM {table} ORDER BY 1")

        return [row[0] for row in rows]

    def last_budget_snapshot_per_account(self) -> dict[str, list[dict[str, Any]]]:
        """For every account ever seen in bsc_acct, the rows of its most recent snapshot."""
        rows = self.connection.execute(
            """
            SELECT b.* FROM budget_snapshots b
            JOIN (SELECT account, MAX(snapshot_ts) AS ts FROM budget_snapshots GROUP BY account) m
              ON b.account = m.account AND b.snapshot_ts = m.ts
            ORDER BY b.account, b.machine
            """
        )
        by_account: dict[str, list[dict[str, Any]]] = {}

        for row in rows:
            by_account.setdefault(row["account"], []).append(dict(row))

        return by_account

    def last_user_usage_per_account(self) -> dict[str, list[UserUsageRow]]:
        rows = self.connection.execute(
            """
            SELECT u.* FROM user_usage_snapshots u
            JOIN (
                SELECT account, MAX(snapshot_ts) AS ts FROM user_usage_snapshots GROUP BY account
            ) m
              ON u.account = m.account AND u.snapshot_ts = m.ts
            """
        )
        by_account: dict[str, list[UserUsageRow]] = {}

        for row in rows:
            by_account.setdefault(row["account"], []).append(
                UserUsageRow(
                    account=row["account"],
                    user=row["user"],
                    full_name=row["full_name"],
                    machine=row["machine"],
                    used_khours=row["used_khours"],
                    pct=row["pct"],
                )
            )

        return by_account

    def association_snapshots(self, snapshot_ts: str) -> list[AssociationSnapshotRow]:
        rows = self.connection.execute(
            "SELECT * FROM associations_snapshots WHERE snapshot_ts = ? ORDER BY account",
            (snapshot_ts,),
        )

        return [
            AssociationSnapshotRow(
                snapshot_ts=row["snapshot_ts"],
                account=row["account"],
                partitions=row["partitions"],
                qos=row["qos"],
                has_association=bool(row["has_association"]),
                in_unix_group=bool(row["in_unix_group"]),
                in_bsc_acct=bool(row["in_bsc_acct"]),
            )
            for row in rows
        ]

    def latest_quota(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM quota_snapshots "
            "WHERE snapshot_ts = (SELECT MAX(snapshot_ts) FROM quota_snapshots)"
        )

        return [dict(row) for row in rows]

    def save_contribution_payload(
        self, payload: dict[str, Any], source: str, imported_at: str
    ) -> bool:
        """Keep the newest contribution per (contributor, project); returns False if not newer."""
        existing = self.connection.execute(
            "SELECT generated_at FROM contributions WHERE contributor = ? AND project = ?",
            (payload["contributor"], payload["project"]),
        ).fetchone()

        if existing and existing["generated_at"] >= payload["generated_at"]:
            return False

        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO contributions VALUES (?, ?, ?, ?, ?, ?)",
                (
                    payload["contributor"],
                    payload["project"],
                    payload["generated_at"],
                    imported_at,
                    source,
                    json.dumps(payload),
                ),
            )

        return True

    def contribution_payloads(self, project: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT payload FROM contributions"
        parameters: tuple[str, ...] = ()

        if project:
            query += " WHERE project = ?"
            parameters = (project,)

        rows = self.connection.execute(query + " ORDER BY contributor", parameters)

        return [json.loads(row["payload"]) for row in rows]

    def write_sync_log(self, entry: SyncLogEntry) -> None:
        values = asdict(entry)
        values["errors"] = json.dumps(entry.errors)
        values["warnings"] = json.dumps(entry.warnings)
        values["ok"] = int(entry.ok)
        columns = ", ".join(values)
        placeholders = ", ".join(f":{name}" for name in values)

        with self.connection:
            self.connection.execute(
                f"INSERT INTO sync_log ({columns}) VALUES ({placeholders})", values
            )

    def last_successful_sync(self) -> str | None:
        return self.connection.execute(
            "SELECT MAX(finished_at) FROM sync_log WHERE ok = 1 AND kind = 'sync'"
        ).fetchone()[0]

    def clear_for_reparse(self) -> None:
        """Drop everything derived from raw outputs; sync_log is kept as the audit trail."""
        with self.connection:
            self.connection.execute("DELETE FROM jobs")

            for table in SNAPSHOT_TABLES:
                self.connection.execute(f"DELETE FROM {table}")
