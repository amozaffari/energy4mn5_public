from dataclasses import replace
from datetime import date

import pytest
from conftest import command_outputs
from test_metrics_attribution import make_job

from mn5_tracker.aggregation import load_attributed_jobs, project_lifetime
from mn5_tracker.attribution import Attribution
from mn5_tracker.config import Config, ConfigError, parse_energy
from mn5_tracker.energy import (
    EnergyInput,
    build_power_model,
    estimate,
    inputs_from_jobs,
    is_exclusive,
)
from mn5_tracker.metrics import compute_usage
from mn5_tracker.reports.dashboard import build_dashboard_data
from mn5_tracker.reports.markdown import build_statement
from mn5_tracker.store import JobRecord, Store
from mn5_tracker.sync import ingest_outputs


TODAY = date(2026, 9, 29)


def record(config: Config, **overrides: object) -> JobRecord:
    """A stored job built from the metrics test's sacct job, with usage computed."""
    job = make_job(**overrides)
    usage = compute_usage(job, config.node_types)

    return JobRecord.from_parts(job, usage, Attribution("geofm", None), "2026-09-29T00:00:00")


def gpp_job(config: Config, cpus: int, energy_j: int | None) -> JobRecord:
    return record(
        config,
        partition="gpp",
        alloc_cpus=cpus,
        alloc_tres=f"billing={cpus},cpu={cpus},node=1",
        consumed_energy_j=energy_j,
    )


def test_only_exclusive_gpp_jobs_count_as_measured(config: Config) -> None:
    full_node = gpp_job(config, 224, 2_400_000)
    shared = gpp_job(config, 2, 2_400_000)

    assert is_exclusive(full_node, config)
    assert not is_exclusive(shared, config)

    measured, modelled = inputs_from_jobs([full_node, shared], config)

    assert measured.measured_kwh == pytest.approx(2_400_000 / 3.6e6)
    assert measured.measured_billed_node_hours == pytest.approx(1.0)
    assert modelled.measured_kwh == 0.0
    assert modelled.billed_node_hours == pytest.approx(2 / 224)


def test_gpp_power_is_calibrated_from_measurements(config: Config) -> None:
    inputs = [EnergyInput("2026-07", "gpp", 20.0, 20.0, 14.0)]

    assert build_power_model(config, inputs).node_power_w("gpp", "central") == pytest.approx(700.0)
    assert build_power_model(config, []).node_power_w("gpp", "central") == pytest.approx(
        config.energy.gpp_node_power_w
    )


def test_acc_scenarios_pue_and_grid(config: Config) -> None:
    """1 ACC node-hour: 4 × 700 W × utilisation + 600 W host, × PUE, × grid intensity."""
    settings = replace(
        config.energy, pue=1.5, grid_g_per_kwh=100.0, grid_g_per_kwh_by_period={"2026": 200.0}
    )
    power = build_power_model(config, [])
    result = estimate([EnergyInput("2026-07", "acc", 1.0)], settings, power)
    other_year = estimate([EnergyInput("2025-07", "acc", 1.0)], settings, power)

    assert result.it_kwh["central"] == pytest.approx((4 * 700 * 0.65 + 600) / 1000)
    assert result.it_kwh["low"] < result.it_kwh["central"] < result.it_kwh["high"]
    assert result.facility_kwh["central"] == pytest.approx(result.it_kwh["central"] * 1.5)
    assert result.co2e_kg["central"] == pytest.approx(result.facility_kwh["central"] * 0.2)
    assert other_year.co2e_kg["central"] == pytest.approx(other_year.facility_kwh["central"] * 0.1)
    assert result.market_co2e_kg is None and result.embodied_co2e_kg is None


def test_market_and_embodied_are_opt_in(config: Config) -> None:
    settings = replace(config.energy, market_g_per_kwh=10.0, embodied_kg_per_node_hour={"acc": 0.5})
    result = estimate([EnergyInput("2026-07", "acc", 2.0)], settings, build_power_model(config, []))

    assert result.market_co2e_kg is not None
    assert result.market_co2e_kg["central"] == pytest.approx(result.facility_kwh["central"] * 0.01)
    assert result.embodied_co2e_kg == pytest.approx(1.0)


def test_month_overrides_year() -> None:
    settings = parse_energy(
        {"grid": {"g_per_kwh": 150, "by_period": {"2026": 120, "2026-01": 180}}}
    )

    assert settings.grid_intensity("2026-01") == 180
    assert settings.grid_intensity("2026-02") == 120
    assert settings.grid_intensity("2024-02") == 150
    assert settings.grid_intensity(None) == 150


def test_utilisation_scenarios_are_required() -> None:
    with pytest.raises(ConfigError, match="scenarios"):
        parse_energy({"acc": {"gpu_utilisation": {"central": 0.5}}})


def test_project_lifetime_energy_covers_bsc_acct_fallback(config: Config, store: Store) -> None:
    ingest_outputs(config, store, "2026-09-29T09:00:00+00:00", command_outputs())
    jobs = load_attributed_jobs(config, store)
    lifetime = project_lifetime(config, store, jobs, "geofm")
    billed = sum(lifetime.lower_bound_billed_node_hours_by_class.values())

    assert lifetime.energy.billed_node_hours == pytest.approx(billed)
    assert lifetime.energy_assumptions["verified"] is False


def test_statement_marks_unverified_energy_as_draft(config: Config, store: Store) -> None:
    ingest_outputs(config, store, "2026-09-29T09:00:00+00:00", command_outputs())
    jobs = load_attributed_jobs(config, store)
    draft = build_statement(config, project_lifetime(config, store, jobs, "geofm"), TODAY)
    verified_config = replace(config, energy=replace(config.energy, verified=True))
    final = build_statement(
        verified_config, project_lifetime(verified_config, store, jobs, "geofm"), TODAY
    )

    assert "do not publish the energy and carbon figures" in draft
    assert "CO₂e" in draft
    assert "do not publish" not in final


def test_dashboard_carries_carbon(config: Config, store: Store) -> None:
    ingest_outputs(config, store, "2026-09-29T09:00:00+00:00", command_outputs())
    carbon = build_dashboard_data(config, store, TODAY)["carbon"]

    assert carbon["verified"] is False
    assert carbon["co2e_kg"]["low"] <= carbon["co2e_kg"]["central"] <= carbon["co2e_kg"]["high"]
    assert carbon["monthly"]
    assert {row["name"] for row in carbon["by_group"]} <= {"geofm", "hydro", "unattributed"}
    assert carbon["measured_it_kwh"] >= 0
    assert carbon["modelled_it_kwh"]["low"] <= carbon["modelled_it_kwh"]["high"]
    assert {row["parameter"].split(" ")[0] for row in carbon["assumptions"]} >= {"PUE", "Grid"}
