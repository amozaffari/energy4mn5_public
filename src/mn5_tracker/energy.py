"""Energy and carbon estimates from billed node-hours, measured where MN5 measures.

Method (Green Algorithms-style, Lannelongue et al. 2021):

    IT energy       = measured IPMI energy (exclusive GPP jobs)
                    + unmeasured billed node-h × modelled node power
    facility energy = IT energy × PUE
    CO2e            = facility energy × grid intensity of the month (location-based)

MN5 records IPMI energy only on GPP nodes, and only per whole node: a job sharing a node reports
the energy of everything on it. Measurements are therefore used for exclusive full-node jobs
only; every other job is modelled. ACC power depends on GPU utilisation, which MN5 does not
record, so ACC estimates come as low / central / high scenarios.
"""

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from mn5_tracker.config import ENERGY_SCENARIOS, Config, EnergySettings
from mn5_tracker.store import JobRecord


MIN_CALIBRATION_NODE_HOURS = 10.0
JOULES_PER_KWH = 3.6e6


@dataclass(frozen=True)
class EnergyInput:
    """A slice of billed usage: one job, one contribution row, or a bsc_acct total."""

    month: str | None
    partition_class: str
    billed_node_hours: float
    measured_billed_node_hours: float = 0.0
    measured_kwh: float = 0.0
    group: str = ""


@dataclass(frozen=True)
class PowerModel:
    """Watts per billed node, per partition class and scenario."""

    watts: dict[str, dict[str, float]]
    gpp_calibration: str

    def node_power_w(self, partition_class: str, scenario: str) -> float:
        return self.watts.get(partition_class, {}).get(scenario, 0.0)


@dataclass
class EnergyEstimate:
    billed_node_hours: float = 0.0
    measured_billed_node_hours: float = 0.0
    measured_it_kwh: float = 0.0
    it_kwh: dict[str, float] = field(default_factory=lambda: dict.fromkeys(ENERGY_SCENARIOS, 0.0))
    facility_kwh: dict[str, float] = field(
        default_factory=lambda: dict.fromkeys(ENERGY_SCENARIOS, 0.0)
    )
    co2e_kg: dict[str, float] = field(default_factory=lambda: dict.fromkeys(ENERGY_SCENARIOS, 0.0))
    market_co2e_kg: dict[str, float] | None = None
    embodied_co2e_kg: float | None = None

    @property
    def measured_share(self) -> float | None:
        """Share of billed node-hours whose energy was measured rather than modelled."""
        if not self.billed_node_hours:
            return None

        return self.measured_billed_node_hours / self.billed_node_hours

    def to_json(self) -> dict[str, object]:
        def rounded(values: dict[str, float] | None, digits: int) -> dict[str, float] | None:
            return {key: round(value, digits) for key, value in values.items()} if values else None

        return {
            "billed_node_hours": round(self.billed_node_hours, 2),
            "measured_share": (
                round(self.measured_share, 4) if self.measured_share is not None else None
            ),
            "measured_it_kwh": round(self.measured_it_kwh, 2),
            "it_kwh": rounded(self.it_kwh, 2),
            "facility_kwh": rounded(self.facility_kwh, 2),
            "co2e_kg": rounded(self.co2e_kg, 2),
            "market_co2e_kg": rounded(self.market_co2e_kg, 2),
            "embodied_co2e_kg": (
                round(self.embodied_co2e_kg, 2) if self.embodied_co2e_kg is not None else None
            ),
        }


def is_exclusive(job: JobRecord, config: Config) -> bool:
    """True when the job held its nodes alone, so the node-level IPMI reading is its own."""
    node_type = config.node_types.get(job.partition_class or "")

    return bool(node_type and job.n_nodes and job.alloc_cpus >= node_type.hw_threads * job.n_nodes)


def inputs_from_jobs(jobs: Iterable[JobRecord], config: Config) -> list[EnergyInput]:
    inputs = []

    for job in jobs:
        if not job.partition_class or job.billed_node_hours <= 0:
            continue

        measured = bool(job.consumed_energy_j) and is_exclusive(job, config)
        inputs.append(
            EnergyInput(
                month=job.start_date.strftime("%Y-%m") if job.start_date else None,
                partition_class=job.partition_class,
                billed_node_hours=job.billed_node_hours,
                measured_billed_node_hours=job.billed_node_hours if measured else 0.0,
                measured_kwh=(job.consumed_energy_j or 0) / JOULES_PER_KWH if measured else 0.0,
            )
        )

    return inputs


def build_power_model(config: Config, calibration_inputs: list[EnergyInput]) -> PowerModel:
    """ACC from GPU power × utilisation + host; GPP from own IPMI measurements when available."""
    settings: EnergySettings = config.energy
    acc_gpus = config.node_types["acc"].gpus if "acc" in config.node_types else 0
    acc = {
        scenario: acc_gpus * settings.acc_gpu_power_w * utilisation + settings.acc_host_power_w
        for scenario, utilisation in settings.acc_gpu_utilisation.items()
    }

    measured_node_hours = sum(
        item.measured_billed_node_hours
        for item in calibration_inputs
        if item.partition_class == "gpp"
    )
    measured_kwh = sum(
        item.measured_kwh for item in calibration_inputs if item.partition_class == "gpp"
    )

    if measured_node_hours >= MIN_CALIBRATION_NODE_HOURS:
        gpp_watts = measured_kwh * 1000 / measured_node_hours
        calibration = (
            f"{gpp_watts:.0f} W/node from {measured_node_hours:,.0f} node-h of exclusive GPP jobs "
            "with IPMI energy"
        )
    else:
        gpp_watts = settings.gpp_node_power_w
        calibration = f"{gpp_watts:.0f} W/node from config (too few measured GPP jobs)"

    return PowerModel(
        watts={"acc": acc, "gpp": dict.fromkeys(ENERGY_SCENARIOS, gpp_watts)},
        gpp_calibration=calibration,
    )


def estimate(
    inputs: Iterable[EnergyInput], settings: EnergySettings, power: PowerModel
) -> EnergyEstimate:
    result = EnergyEstimate()
    market: dict[str, float] = dict.fromkeys(ENERGY_SCENARIOS, 0.0)
    embodied = 0.0

    for item in inputs:
        unmeasured = max(item.billed_node_hours - item.measured_billed_node_hours, 0.0)
        intensity_kg_per_kwh = settings.grid_intensity(item.month) / 1000
        result.billed_node_hours += item.billed_node_hours
        result.measured_billed_node_hours += item.measured_billed_node_hours
        result.measured_it_kwh += item.measured_kwh
        embodied += item.billed_node_hours * settings.embodied_kg_per_node_hour.get(
            item.partition_class, 0.0
        )

        for scenario in ENERGY_SCENARIOS:
            it_kwh = (
                item.measured_kwh
                + unmeasured * power.node_power_w(item.partition_class, scenario) / 1000
            )
            facility_kwh = it_kwh * settings.pue
            result.it_kwh[scenario] += it_kwh
            result.facility_kwh[scenario] += facility_kwh
            result.co2e_kg[scenario] += facility_kwh * intensity_kg_per_kwh

            if settings.market_g_per_kwh is not None:
                market[scenario] += facility_kwh * settings.market_g_per_kwh / 1000

    result.market_co2e_kg = market if settings.market_g_per_kwh is not None else None
    result.embodied_co2e_kg = embodied if settings.embodied_kg_per_node_hour else None

    return result


def estimate_by(
    inputs: list[EnergyInput],
    settings: EnergySettings,
    power: PowerModel,
    key: Callable[[EnergyInput], str],
) -> dict[str, EnergyEstimate]:
    grouped: dict[str, list[EnergyInput]] = defaultdict(list)

    for item in inputs:
        grouped[key(item)].append(item)

    return {name: estimate(items, settings, power) for name, items in sorted(grouped.items())}


def assumptions(config: Config, power: PowerModel) -> dict[str, object]:
    """Every parameter behind an estimate, for JSON output and statements."""
    settings = config.energy

    return {
        "verified": settings.verified,
        "pue": settings.pue,
        "acc_node_power_w": {name: round(value) for name, value in power.watts["acc"].items()},
        "gpp_node_power_w": round(power.watts["gpp"]["central"]),
        "gpp_calibration": power.gpp_calibration,
        "grid_g_per_kwh": settings.grid_g_per_kwh,
        "grid_g_per_kwh_by_period": settings.grid_g_per_kwh_by_period,
        "market_g_per_kwh": settings.market_g_per_kwh,
        "embodied_kg_per_node_hour": settings.embodied_kg_per_node_hour or None,
        "sources": settings.sources,
    }
