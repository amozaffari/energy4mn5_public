"""Aggregate stored jobs and bsc_acct snapshots into report rows and project lifetimes."""

import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, replace
from datetime import date

from mn5_tracker.attribution import attribute
from mn5_tracker.config import Config
from mn5_tracker.contributions import Contribution, energy_inputs, load_contributions
from mn5_tracker.energy import (
    EnergyEstimate,
    EnergyInput,
    assumptions,
    build_power_model,
    estimate,
    inputs_from_jobs,
)
from mn5_tracker.metrics import khours_to_node_hours
from mn5_tracker.store import JobRecord, Store


WASTED_STATES = frozenset(
    {"FAILED", "CANCELLED", "OUT_OF_MEMORY", "NODE_FAIL", "BOOT_FAIL", "DEADLINE", "PREEMPTED"}
)
GROUP_BY_OPTIONS = (
    "month",
    "account",
    "project",
    "state",
    "queue",
    "jobname-prefix",
    "partition",
    "workdir",
)
DEVELOPMENT = "development"
PRODUCTION = "production"
JOULES_PER_KWH = 3.6e6
TRAILING_JOB_SUFFIX = re.compile(r"([_\-.]?(\d+|v\d+|sh|slurm|job))+$", re.IGNORECASE)

OWN_JOBS_CAVEAT = "sacct shows only this user's own jobs; colleagues' jobs are not included."
OCCUPANCY_CAVEAT = (
    "node_hours counts nodes occupied (ElapsedRaw × NNodes); billed_node_hours is what the budget "
    "was charged (1-GPU ACC jobs are billed a quarter node). GPU-h headline = billed."
)


@dataclass(frozen=True)
class JobFilter:
    project: str | None = None
    account: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    partition_class: str | None = None

    def matches(self, job: JobRecord) -> bool:
        started = job.start_date

        if self.project and job.project != self.project:
            return False

        if self.account and job.account != self.account:
            return False

        if self.partition_class and job.partition_class != self.partition_class:
            return False

        if self.date_from and (started is None or started < self.date_from):
            return False

        return not (self.date_to and (started is None or started > self.date_to))


@dataclass
class UsageRow:
    key: str
    jobs: int = 0
    completed: int = 0
    wasted_jobs: int = 0
    debug_failed_jobs: int = 0
    production_jobs: int = 0
    production_completed: int = 0
    production_active: int = 0
    timeout_jobs: int = 0
    active_jobs: int = 0
    node_hours: float = 0.0
    billed_node_hours: float = 0.0
    gpu_hours_billed: float = 0.0
    gpu_hours_node: float = 0.0
    gpu_hours_requested: float = 0.0
    core_hours_billed: float = 0.0
    wasted_billed_node_hours: float = 0.0
    debug_failed_billed_node_hours: float = 0.0
    timeout_billed_node_hours: float = 0.0
    acc_billed_node_hours: float = 0.0
    gpp_billed_node_hours: float = 0.0
    energy_kwh: float = 0.0
    energy_jobs: int = 0
    first_start: str | None = None
    last_start: str | None = None

    @property
    def success_rate(self) -> float | None:
        finished = self.jobs - self.active_jobs

        return self.completed / finished if finished else None

    @property
    def production_success_rate(self) -> float | None:
        """Completed share of finished jobs in production queues (debug queues excluded)."""
        finished = self.production_jobs - self.production_active

        return self.production_completed / finished if finished else None

    @property
    def khours_billed(self) -> float:
        return self.core_hours_billed / 1000

    def add(self, job: JobRecord) -> None:
        self.jobs += 1
        self.node_hours += job.node_hours
        self.billed_node_hours += job.billed_node_hours
        self.gpu_hours_billed += job.gpu_hours_billed
        self.gpu_hours_node += job.gpu_hours_node
        self.gpu_hours_requested += job.gpu_hours_requested
        self.core_hours_billed += job.core_hours_billed

        if job.partition_class == "acc":
            self.acc_billed_node_hours += job.billed_node_hours
        elif job.partition_class == "gpp":
            self.gpp_billed_node_hours += job.billed_node_hours

        production = job.queue != DEVELOPMENT

        if production:
            self.production_jobs += 1

        if job.state == "COMPLETED":
            self.completed += 1
            self.production_completed += int(production)
        elif job.state == "TIMEOUT":
            self.timeout_jobs += 1
            self.timeout_billed_node_hours += job.billed_node_hours
        elif job.state in WASTED_STATES and production:
            self.wasted_jobs += 1
            self.wasted_billed_node_hours += job.billed_node_hours
        elif job.state in WASTED_STATES:
            # Failing in a debug queue is part of development, not waste.
            self.debug_failed_jobs += 1
            self.debug_failed_billed_node_hours += job.billed_node_hours
        elif not job.is_terminal:
            self.active_jobs += 1
            self.production_active += int(production)

        if job.consumed_energy_j:
            self.energy_kwh += job.consumed_energy_j / JOULES_PER_KWH
            self.energy_jobs += 1

        if job.start:
            self.first_start = min(filter(None, (self.first_start, job.start)))
            self.last_start = max(filter(None, (self.last_start, job.start)))

    def to_json(self) -> dict[str, object]:
        return {
            "key": self.key,
            "jobs": self.jobs,
            "completed": self.completed,
            "wasted_jobs": self.wasted_jobs,
            "debug_failed_jobs": self.debug_failed_jobs,
            "timeout_jobs": self.timeout_jobs,
            "active_jobs": self.active_jobs,
            "success_rate": round(self.success_rate, 4) if self.success_rate is not None else None,
            "production_success_rate": (
                round(self.production_success_rate, 4)
                if self.production_success_rate is not None
                else None
            ),
            "node_hours": round(self.node_hours, 2),
            "billed_node_hours": round(self.billed_node_hours, 2),
            "acc_billed_node_hours": round(self.acc_billed_node_hours, 2),
            "gpp_billed_node_hours": round(self.gpp_billed_node_hours, 2),
            "gpu_hours_billed": round(self.gpu_hours_billed, 2),
            "gpu_hours_node": round(self.gpu_hours_node, 2),
            "gpu_hours_requested": round(self.gpu_hours_requested, 2),
            "core_hours_billed": round(self.core_hours_billed, 1),
            "khours_billed": round(self.khours_billed, 3),
            "wasted_billed_node_hours": round(self.wasted_billed_node_hours, 2),
            "debug_failed_billed_node_hours": round(self.debug_failed_billed_node_hours, 2),
            "timeout_billed_node_hours": round(self.timeout_billed_node_hours, 2),
            "energy_kwh": round(self.energy_kwh, 2),
            "energy_jobs": self.energy_jobs,
            "first_start": self.first_start,
            "last_start": self.last_start,
        }


def load_attributed_jobs(config: Config, store: Store) -> list[JobRecord]:
    """Jobs with attribution and queue kind re-applied from the current config, so edits to
    rules or `development_qos` take effect at once."""
    jobs = []

    for job in store.load_jobs():
        attribution = attribute(job, config.attribution_rules)
        queue = DEVELOPMENT if job.qos in config.development_qos else PRODUCTION
        jobs.append(
            replace(
                job,
                project=attribution.project,
                attribution_rule=attribution.rule_index,
                queue=queue,
            )
        )

    return jobs


def job_family(job_name: str) -> str:
    """Strip run counters and script suffixes: "train_unet_v2_0042.sh" → "train_unet"."""
    family = TRAILING_JOB_SUFFIX.sub("", job_name)

    return family or job_name


def workdir_key(work_dir: str) -> str:
    """Project-level directory: the part after the user's scratch root, two levels deep."""
    trimmed = re.sub(r"^/gpfs/(scratch|projects|home)/[^/]+/(bsc\d+/)?", "", work_dir)

    return "/".join(trimmed.split("/")[:2]) or work_dir


GROUP_KEYS: dict[str, Callable[[JobRecord], str]] = {
    "month": lambda job: job.start_date.strftime("%Y-%m") if job.start_date else "not started",
    "account": lambda job: job.account,
    "project": lambda job: job.project,
    "state": lambda job: job.state,
    "queue": lambda job: job.queue,
    "jobname-prefix": lambda job: job_family(job.job_name),
    "partition": lambda job: job.partition_class or job.partition,
    "workdir": lambda job: workdir_key(job.work_dir),
}


def aggregate(jobs: Iterable[JobRecord], by: str | None) -> list[UsageRow]:
    if by is not None and by not in GROUP_KEYS:
        raise ValueError(f"Unknown group-by {by!r}; choose from {', '.join(GROUP_BY_OPTIONS)}")

    rows: dict[str, UsageRow] = {}

    for job in jobs:
        key = GROUP_KEYS[by](job) if by else "total"
        rows.setdefault(key, UsageRow(key=key)).add(job)

    ordered = sorted(
        rows.values(), key=lambda row: row.key if by == "month" else -row.billed_node_hours
    )

    return ordered


def total(jobs: Iterable[JobRecord]) -> UsageRow:
    rows = aggregate(jobs, None)

    return rows[0] if rows else UsageRow(key="total")


def top_jobs(jobs: list[JobRecord], limit: int) -> list[JobRecord]:
    return sorted(jobs, key=lambda job: job.billed_node_hours, reverse=True)[:limit]


@dataclass
class AllocationUsage:
    """One allocation's share of a project: own jobs + contributed + bsc_acct fallback."""

    account: str
    title: str
    kind: str
    shared: bool
    own_jobs: int
    own_node_hours: float
    own_billed_node_hours_by_class: dict[str, float]
    contributed_billed_node_hours_by_class: dict[str, float]
    contributors: list[str]
    bsc_acct_others_billed_node_hours_by_class: dict[str, float] | None
    bsc_acct_others_users: int
    others_source: str
    last_snapshot_ts: str | None
    visible_now: bool

    @property
    def total_billed_node_hours_by_class(self) -> dict[str, float]:
        return merge_by_class(
            self.own_billed_node_hours_by_class,
            self.contributed_billed_node_hours_by_class,
            self.bsc_acct_others_billed_node_hours_by_class or {},
        )

    @property
    def others_billed_node_hours_by_class(self) -> dict[str, float]:
        return merge_by_class(
            self.contributed_billed_node_hours_by_class,
            self.bsc_acct_others_billed_node_hours_by_class or {},
        )

    def to_json(self) -> dict[str, object]:
        fallback = self.bsc_acct_others_billed_node_hours_by_class

        return {
            "account": self.account,
            "title": self.title,
            "kind": self.kind,
            "shared": self.shared,
            "own_jobs": self.own_jobs,
            "own_node_hours": round(self.own_node_hours, 2),
            "own_billed_node_hours_by_class": rounded(self.own_billed_node_hours_by_class),
            "contributed_billed_node_hours_by_class": rounded(
                self.contributed_billed_node_hours_by_class
            ),
            "contributors": self.contributors,
            "bsc_acct_others_billed_node_hours_by_class": (
                rounded(fallback) if fallback is not None else None
            ),
            "bsc_acct_others_users": self.bsc_acct_others_users,
            "total_billed_node_hours_by_class": rounded(self.total_billed_node_hours_by_class),
            "others_source": self.others_source,
            "last_snapshot_ts": self.last_snapshot_ts,
            "visible_now": self.visible_now,
        }


@dataclass(frozen=True)
class ContributorSummary:
    contributor: str
    generated_at: str
    jobs: int
    billed_node_hours_by_class: dict[str, float]
    same_rules: bool


@dataclass
class ProjectLifetime:
    project: str
    title: str
    own: UsageRow
    contributors: list[ContributorSummary]
    allocations: list[AllocationUsage]
    invisible: list[dict[str, str]]
    gpus_per_node: dict[str, int]
    monthly_billed_node_hours: dict[str, dict[str, float]]
    energy: EnergyEstimate = field(default_factory=EnergyEstimate)
    energy_assumptions: dict[str, object] = field(default_factory=dict)
    caveats: list[str] = field(default_factory=list)

    @property
    def lower_bound_billed_node_hours_by_class(self) -> dict[str, float]:
        return merge_by_class(
            *(allocation.total_billed_node_hours_by_class for allocation in self.allocations)
        )

    @property
    def others_billed_node_hours_by_class(self) -> dict[str, float]:
        return merge_by_class(
            *(allocation.others_billed_node_hours_by_class for allocation in self.allocations)
        )

    @property
    def members_with_detail(self) -> int:
        """You plus every member whose contribution was imported."""
        return 1 + len(self.contributors)

    @property
    def members_from_bsc_acct_only(self) -> int:
        return max((allocation.bsc_acct_others_users for allocation in self.allocations), default=0)

    @property
    def first_start(self) -> str | None:
        months = sorted(self.monthly_billed_node_hours)

        return self.own.first_start or (f"{months[0]}-01" if months else None)

    @property
    def last_start(self) -> str | None:
        months = sorted(self.monthly_billed_node_hours)
        candidates = [
            value for value in (self.own.last_start, months[-1] if months else None) if value
        ]

        return max(candidates) if candidates else None

    def gpu_hours(self, hours_by_class: dict[str, float]) -> float:
        return sum(
            hours * self.gpus_per_node.get(name, 0) for name, hours in hours_by_class.items()
        )

    def to_json(self) -> dict[str, object]:
        lower_bound = self.lower_bound_billed_node_hours_by_class
        others = self.others_billed_node_hours_by_class

        return {
            "schema": "mn5track.project_lifetime/v2",
            "project": self.project,
            "title": self.title,
            "own": self.own.to_json(),
            "contributors": [asdict(contributor) for contributor in self.contributors],
            "coverage": {
                "members_with_detail": self.members_with_detail,
                "members_from_bsc_acct_only": self.members_from_bsc_acct_only,
            },
            "others_billed_node_hours_by_class": rounded(others),
            "others_gpu_hours_billed": round(self.gpu_hours(others), 1),
            "lower_bound_billed_node_hours_by_class": rounded(lower_bound),
            "lower_bound_gpu_hours_billed": round(self.gpu_hours(lower_bound), 1),
            "monthly_billed_node_hours": {
                month: rounded(values) for month, values in self.monthly_billed_node_hours.items()
            },
            "energy": {**self.energy.to_json(), "assumptions": self.energy_assumptions},
            "allocations": [allocation.to_json() for allocation in self.allocations],
            "invisible_allocations": self.invisible,
            "caveats": self.caveats,
        }


def rounded(values: dict[str, float]) -> dict[str, float]:
    return {key: round(value, 2) for key, value in values.items()}


def merge_by_class(*parts: dict[str, float]) -> dict[str, float]:
    combined: dict[str, float] = defaultdict(float)

    for part in parts:
        for node_class, hours in part.items():
            combined[node_class] += hours

    return dict(combined)


def billed_by_class(jobs: Iterable[JobRecord]) -> dict[str, float]:
    by_class: dict[str, float] = defaultdict(float)

    for job in jobs:
        if job.partition_class:
            by_class[job.partition_class] += job.billed_node_hours

    return dict(by_class)


def bsc_acct_fallback(
    config: Config, store: Store, account: str, excluded_users: set[str]
) -> tuple[dict[str, float] | None, int, str | None]:
    """Usage of members who have not contributed, from the allocation's last bsc_acct snapshot.

    Returns (node-hours by class, number of such members with usage, snapshot timestamp).
    """
    usage_rows = store.last_user_usage_per_account().get(account)

    if not usage_rows:
        return None, 0, None

    by_class: dict[str, float] = defaultdict(float)
    users_with_usage = set()

    for usage in usage_rows:
        node_type = config.node_type_for_machine(usage.machine)

        if usage.user in excluded_users or node_type is None:
            continue

        by_class[node_type.name] += khours_to_node_hours(usage.used_khours, node_type)

        if usage.used_khours > 0:
            users_with_usage.add(usage.user)

    snapshot_ts = store.last_budget_snapshot_per_account()[account][0]["snapshot_ts"]

    return dict(by_class), len(users_with_usage), snapshot_ts


def monthly_usage(
    jobs: list[JobRecord], contributions: list[Contribution]
) -> dict[str, dict[str, float]]:
    """Billed node-h per month and class, from members with detailed data (no bsc_acct)."""
    monthly: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    for job in jobs:
        if job.start_date and job.partition_class:
            monthly[job.start_date.strftime("%Y-%m")][job.partition_class] += job.billed_node_hours

    for contribution in contributions:
        for row in contribution.rows:
            if row.month[:4].isdigit():
                monthly[row.month][row.partition_class] += row.billed_node_hours

    return {month: dict(values) for month, values in sorted(monthly.items())}


def covering_contributors(contributions: list[Contribution], account: str) -> set[str]:
    """Contributors whose sacct history includes this allocation (any project)."""
    return {
        contribution.contributor
        for contribution in contributions
        if any(total.account == account for total in contribution.account_totals)
    }


def summarise_contributors(
    config: Config, contributions: list[Contribution]
) -> list[ContributorSummary]:
    return [
        ContributorSummary(
            contributor=contribution.contributor,
            generated_at=contribution.generated_at,
            jobs=contribution.jobs,
            billed_node_hours_by_class=rounded(contribution.billed_node_hours_by_class()),
            same_rules=contribution.rules_fingerprint == config.rules_fingerprint,
        )
        for contribution in contributions
    ]


def project_lifetime(
    config: Config, store: Store, jobs: list[JobRecord], project: str
) -> ProjectLifetime:
    """Per allocation: own jobs + imported contributions + bsc_acct rows of everyone else.

    Combining per person avoids double counting: on each allocation, a member whose own sacct
    saw that allocation is counted from their contribution and dropped from the bsc_acct
    fallback. Members who did not contribute, or whose file does not cover the allocation, keep
    their bsc_acct figure, so an incomplete file can never make usage disappear.
    """
    project_jobs = [job for job in jobs if job.project == project]
    project_config = config.projects.get(project)
    contributions = load_contributions(store, project, config.user)
    hinted = set(project_config.allocations_hint) if project_config else set()
    contributed_accounts = {row.account for item in contributions for row in item.rows}
    accounts = sorted(hinted | contributed_accounts | {job.account for job in project_jobs})
    budget_timestamps = store.snapshot_timestamps("budget_snapshots")
    latest_ts = budget_timestamps[-1] if budget_timestamps else None

    allocations = []
    invisible = []

    for account in accounts:
        allocation = config.allocations.get(account)
        shared = bool(allocation and allocation.shared)
        account_jobs = [job for job in project_jobs if job.account == account]
        account_contributors = sorted(
            item.contributor for item in contributions if item.billed_node_hours_by_class(account)
        )
        fallback, fallback_users, snapshot_ts = (None, 0, None)
        visible_now = False

        if shared:
            others_source = "shared allocation: only members' own attributed jobs are counted"
        elif account not in hinted:
            others_source = "not in allocations_hint: bsc_acct totals not counted"
        else:
            fallback, fallback_users, snapshot_ts = bsc_acct_fallback(
                config,
                store,
                account,
                {config.user, *covering_contributors(contributions, account)},
            )
            visible_now = snapshot_ts is not None and snapshot_ts == latest_ts
            others_source = describe_snapshot_source(snapshot_ts, visible_now)

        if account in hinted and not shared and fallback is None:
            invisible.append(
                {
                    "account": account,
                    "reason": "never seen in bsc_acct: only members with detailed data are "
                    f"counted (you + {len(account_contributors)} contributors here)",
                }
            )
        elif fallback is not None and not visible_now and snapshot_ts:
            invisible.append(
                {
                    "account": account,
                    "reason": f"no longer visible in bsc_acct; final snapshot of "
                    f"{snapshot_ts[:10]} is used for members who have not contributed",
                }
            )

        allocations.append(
            AllocationUsage(
                account=account,
                title=allocation.title if allocation else account,
                kind=allocation.kind if allocation else "unknown",
                shared=shared,
                own_jobs=len(account_jobs),
                own_node_hours=sum(job.node_hours for job in account_jobs),
                own_billed_node_hours_by_class=billed_by_class(account_jobs),
                contributed_billed_node_hours_by_class=merge_by_class(
                    *(item.billed_node_hours_by_class(account) for item in contributions)
                ),
                contributors=account_contributors,
                bsc_acct_others_billed_node_hours_by_class=fallback,
                bsc_acct_others_users=fallback_users,
                others_source=others_source,
                last_snapshot_ts=snapshot_ts,
                visible_now=visible_now,
            )
        )

    fallback_inputs = [
        EnergyInput(month=None, partition_class=node_class, billed_node_hours=hours)
        for allocation in allocations
        for node_class, hours in (
            allocation.bsc_acct_others_billed_node_hours_by_class or {}
        ).items()
    ]
    power = build_power_model(config, inputs_from_jobs(jobs, config))
    project_energy = estimate(
        [*inputs_from_jobs(project_jobs, config), *energy_inputs(contributions), *fallback_inputs],
        config.energy,
        power,
    )

    return ProjectLifetime(
        project=project,
        title=project_config.title if project_config else project,
        own=total(project_jobs),
        contributors=summarise_contributors(config, contributions),
        allocations=allocations,
        invisible=invisible,
        gpus_per_node={name: node_type.gpus for name, node_type in config.node_types.items()},
        monthly_billed_node_hours=monthly_usage(project_jobs, contributions),
        energy=project_energy,
        energy_assumptions=assumptions(config, power),
        caveats=[
            "Lower bound: own jobs and imported contributions (detailed, from each member's "
            "sacct) plus bsc_acct totals of members who have not contributed, on dedicated "
            "allocations only.",
            "On shared allocations (e.g. bsc32) only jobs attributed by members with detailed "
            "data are counted.",
            "Monthly figures cover members with detailed data only.",
            OCCUPANCY_CAVEAT,
        ],
    )


def describe_snapshot_source(snapshot_ts: str | None, visible_now: bool) -> str:
    if snapshot_ts is None:
        return "no bsc_acct snapshot: non-contributors' usage unknown"

    if visible_now:
        return f"bsc_acct snapshot {snapshot_ts[:10]} (current)"

    return f"bsc_acct final snapshot {snapshot_ts[:10]} (no longer visible)"
