"""Load and validate `tracker.yaml` into typed, immutable configuration objects."""

import hashlib
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml


CONFIG_ENV_VAR = "MN5TRACK_CONFIG"
DB_ENV_VAR = "MN5TRACK_DB"
DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "mn5-tracker" / "tracker.db"
REPO_ROOT = Path(__file__).resolve().parents[2]
SHARED_CONFIG_NAME = "project.yaml"


class ConfigError(ValueError):
    """Raised when `tracker.yaml` is missing or inconsistent."""


@dataclass(frozen=True)
class Hosts:
    primary: str
    fallback: str
    connect_timeout_s: int = 15
    command_timeout_s: int = 300


@dataclass(frozen=True)
class NodeType:
    name: str
    partition_prefixes: tuple[str, ...]
    bsc_acct_machine: str
    physical_cores: int
    hw_threads: int
    gpus: int = 0
    gpu_model: str | None = None


@dataclass(frozen=True)
class Allocation:
    account: str
    title: str
    kind: str
    role: str = "compute"
    shared: bool = False
    start: date | None = None
    end: date | None = None
    note: str = ""


@dataclass(frozen=True)
class Project:
    name: str
    title: str
    allocations_hint: tuple[str, ...] = ()


@dataclass(frozen=True)
class AttributionRule:
    index: int
    project: str
    workdir_glob: str | None = None
    jobname_glob: str | None = None
    account_in: tuple[str, ...] | None = None
    submitted_from: date | None = None
    submitted_until: date | None = None

    def describe(self) -> str:
        """Human-readable one-liner used by `explain` and the attribution skill."""
        conditions = []

        if self.workdir_glob:
            conditions.append(f"workdir_glob={self.workdir_glob!r}")

        if self.jobname_glob:
            conditions.append(f"jobname_glob={self.jobname_glob!r}")

        if self.account_in:
            conditions.append(f"account_in={list(self.account_in)}")

        if self.submitted_from:
            conditions.append(f"submitted_from={self.submitted_from}")

        if self.submitted_until:
            conditions.append(f"submitted_until={self.submitted_until}")

        return f"#{self.index} → {self.project}: " + ", ".join(conditions)


@dataclass(frozen=True)
class AlertThresholds:
    used_pct_warn: float = 80.0
    expiry_days_warn: int = 30
    stale_sync_days: int = 3


@dataclass(frozen=True)
class SyncSettings:
    overlap_days: int = 7
    include_quota: bool = True


ENERGY_SCENARIOS = ("low", "central", "high")
DEFAULT_DEVELOPMENT_QOS = ("acc_debug", "gp_debug", "acc_interactive", "gp_interactive")


@dataclass(frozen=True)
class DashboardSettings:
    """Where `dashboard --all` writes, and which project views it builds besides the own view."""

    out_dir: Path | None = None
    projects: tuple[str, ...] = ()


@dataclass(frozen=True)
class EnergySettings:
    """Parameters of the energy and carbon model; each should cite a source in project.yaml."""

    pue: float = 1.2
    acc_gpu_power_w: float = 700.0
    acc_host_power_w: float = 600.0
    acc_gpu_utilisation: dict[str, float] = field(
        default_factory=lambda: {"low": 0.4, "central": 0.65, "high": 0.9}
    )
    gpp_node_power_w: float = 663.0
    grid_g_per_kwh: float = 150.0
    grid_g_per_kwh_by_period: dict[str, float] = field(default_factory=dict)
    market_g_per_kwh: float | None = None
    embodied_kg_per_node_hour: dict[str, float] = field(default_factory=dict)
    verified: bool = False
    sources: dict[str, str] = field(default_factory=dict)

    def grid_intensity(self, month: str | None) -> float:
        """g CO2e/kWh for "YYYY-MM": exact month, then its year, then the default."""
        if month and month in self.grid_g_per_kwh_by_period:
            return self.grid_g_per_kwh_by_period[month]

        if month and month[:4] in self.grid_g_per_kwh_by_period:
            return self.grid_g_per_kwh_by_period[month[:4]]

        return self.grid_g_per_kwh


@dataclass(frozen=True)
class Config:
    user: str
    first_job_date: date
    hosts: Hosts
    node_types: dict[str, NodeType]
    allocations: dict[str, Allocation]
    projects: dict[str, Project]
    attribution_rules: tuple[AttributionRule, ...]
    alerts: AlertThresholds = field(default_factory=AlertThresholds)
    sync: SyncSettings = field(default_factory=SyncSettings)
    energy: EnergySettings = field(default_factory=EnergySettings)
    development_qos: tuple[str, ...] = DEFAULT_DEVELOPMENT_QOS
    dashboards: DashboardSettings = field(default_factory=DashboardSettings)
    source_path: Path | None = None
    shared_path: Path | None = None

    @property
    def rules_fingerprint(self) -> str:
        """Short hash of the attribution rules, so contributions made with other rules stand out."""
        described = "\n".join(rule.describe() for rule in self.attribution_rules)

        return hashlib.sha256(described.encode()).hexdigest()[:12]

    def node_type_for_machine(self, machine: str) -> NodeType | None:
        """Map a `bsc_acct` machine label (e.g. "Marenostrum5 ACC") to its node type."""
        for node_type in self.node_types.values():
            if node_type.bsc_acct_machine == machine:
                return node_type

        return None


def find_config_path() -> Path:
    """Resolve the config file: env var, then ./config, then the repository's config."""
    env_path = os.environ.get(CONFIG_ENV_VAR)

    if env_path:
        return Path(env_path).expanduser()

    candidates = [Path.cwd() / "config" / "tracker.yaml", REPO_ROOT / "config" / "tracker.yaml"]

    for candidate in candidates:
        if candidate.exists():
            return candidate

    raise ConfigError(
        f"No tracker.yaml found; set {CONFIG_ENV_VAR} or copy config/tracker.example.yaml "
        "to config/tracker.yaml"
    )


def find_db_path() -> Path:
    return Path(os.environ.get(DB_ENV_VAR, DEFAULT_DB_PATH)).expanduser()


def find_shared_config_path(personal: dict[str, Any], personal_path: Path) -> Path | None:
    """Shared project file: explicit `project_config`, else project.yaml beside the personal one."""
    if explicit := personal.get("project_config"):
        return personal_path.parent / Path(explicit).expanduser()

    candidates = [
        personal_path.parent / SHARED_CONFIG_NAME,
        REPO_ROOT / "config" / SHARED_CONFIG_NAME,
    ]

    return next((candidate for candidate in candidates if candidate.exists()), None)


def read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    return yaml.safe_load(path.read_text()) or {}


def load_config(path: Path | None = None) -> Config:
    """Merge the shared project file with the personal one; personal keys win.

    Personal `extra_attribution_rules` are checked before the shared rules, so a member with an
    unusual directory layout can map their jobs without changing everyone's rules.
    """
    personal_path = path or find_config_path()
    personal = read_yaml(personal_path)
    shared_path = find_shared_config_path(personal, personal_path)
    shared = read_yaml(shared_path) if shared_path else {}

    merged = {**shared, **personal}
    merged["attribution_rules"] = [
        *(personal.get("extra_attribution_rules") or []),
        *(merged.get("attribution_rules") or []),
    ]

    return parse_config(merged, source_path=personal_path, shared_path=shared_path)


def parse_config(
    raw: dict[str, Any], source_path: Path | None = None, shared_path: Path | None = None
) -> Config:
    missing = [key for key in ("user", "hosts", "node_types") if key not in raw]

    if missing:
        raise ConfigError(f"config (tracker.yaml + project.yaml) is missing keys: {missing}")

    node_types = {
        name: NodeType(
            name=name,
            partition_prefixes=tuple(spec.get("partition_prefixes", [name])),
            bsc_acct_machine=spec["bsc_acct_machine"],
            physical_cores=int(spec["physical_cores"]),
            hw_threads=int(spec.get("hw_threads", spec["physical_cores"])),
            gpus=int(spec.get("gpus", 0)),
            gpu_model=spec.get("gpu_model"),
        )
        for name, spec in raw["node_types"].items()
    }

    allocations = {
        account: Allocation(
            account=account,
            title=spec.get("title", account),
            kind=spec.get("kind", "unknown"),
            role=spec.get("role", "compute"),
            shared=bool(spec.get("shared", False)),
            start=spec.get("start"),
            end=spec.get("end"),
            note=spec.get("note", ""),
        )
        for account, spec in (raw.get("allocations") or {}).items()
    }

    projects = {
        name: Project(
            name=name,
            title=(spec or {}).get("title", name),
            allocations_hint=tuple((spec or {}).get("allocations_hint", [])),
        )
        for name, spec in (raw.get("projects") or {}).items()
    }

    rules = tuple(
        parse_rule(index, spec) for index, spec in enumerate(raw.get("attribution_rules") or [], 1)
    )

    return Config(
        user=raw["user"],
        first_job_date=raw.get("first_job_date", date(2024, 1, 1)),
        hosts=Hosts(**raw["hosts"]),
        node_types=node_types,
        allocations=allocations,
        projects=projects,
        attribution_rules=rules,
        alerts=AlertThresholds(**(raw.get("alerts") or {})),
        sync=SyncSettings(**(raw.get("sync") or {})),
        energy=parse_energy(raw.get("energy") or {}),
        development_qos=tuple(
            (raw.get("queues") or {}).get("development_qos", DEFAULT_DEVELOPMENT_QOS)
        ),
        dashboards=parse_dashboards(raw.get("dashboards") or {}, projects),
        source_path=source_path,
        shared_path=shared_path,
    )


def parse_dashboards(spec: dict[str, Any], projects: dict[str, "Project"]) -> DashboardSettings:
    names = tuple(spec.get("projects") or ())
    unknown = [name for name in names if name not in projects]

    if unknown:
        raise ConfigError(f"dashboards.projects lists unknown projects: {unknown}")

    out_dir = spec.get("out_dir")

    return DashboardSettings(
        out_dir=Path(out_dir).expanduser() if out_dir else None, projects=names
    )


def parse_energy(spec: dict[str, Any]) -> EnergySettings:
    acc = spec.get("acc") or {}
    gpp = spec.get("gpp") or {}
    grid = spec.get("grid") or {}
    defaults = EnergySettings()
    utilisation = acc.get("gpu_utilisation", defaults.acc_gpu_utilisation)
    missing = [name for name in ENERGY_SCENARIOS if name not in utilisation]

    if missing:
        raise ConfigError(f"energy.acc.gpu_utilisation is missing scenarios: {missing}")

    return EnergySettings(
        pue=float(spec.get("pue", defaults.pue)),
        acc_gpu_power_w=float(acc.get("gpu_power_w", defaults.acc_gpu_power_w)),
        acc_host_power_w=float(acc.get("host_power_w", defaults.acc_host_power_w)),
        acc_gpu_utilisation={name: float(value) for name, value in utilisation.items()},
        gpp_node_power_w=float(gpp.get("node_power_w", defaults.gpp_node_power_w)),
        grid_g_per_kwh=float(grid.get("g_per_kwh", defaults.grid_g_per_kwh)),
        grid_g_per_kwh_by_period={
            str(period): float(value) for period, value in (grid.get("by_period") or {}).items()
        },
        market_g_per_kwh=(
            float(grid["market_g_per_kwh"]) if grid.get("market_g_per_kwh") is not None else None
        ),
        embodied_kg_per_node_hour={
            name: float(value)
            for name, value in (spec.get("embodied_kg_per_node_hour") or {}).items()
        },
        verified=bool(spec.get("verified", False)),
        sources={str(key): str(value) for key, value in (spec.get("sources") or {}).items()},
    )


def parse_rule(index: int, spec: dict[str, Any]) -> AttributionRule:
    condition_keys = {
        "workdir_glob",
        "jobname_glob",
        "account_in",
        "submitted_from",
        "submitted_until",
    }
    unknown_keys = set(spec) - condition_keys - {"project"}

    if "project" not in spec:
        raise ConfigError(f"attribution rule #{index} has no 'project'")

    if unknown_keys:
        raise ConfigError(f"attribution rule #{index} has unknown keys: {sorted(unknown_keys)}")

    if not condition_keys & set(spec):
        raise ConfigError(f"attribution rule #{index} has no conditions and would match every job")

    account_in = spec.get("account_in")

    return AttributionRule(
        index=index,
        project=spec["project"],
        workdir_glob=spec.get("workdir_glob"),
        jobname_glob=spec.get("jobname_glob"),
        account_in=tuple(account_in) if account_in else None,
        submitted_from=spec.get("submitted_from"),
        submitted_until=spec.get("submitted_until"),
    )
