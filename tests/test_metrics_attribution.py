from dataclasses import replace
from datetime import date

import pytest

from mn5_tracker.attribution import UNATTRIBUTED, attribute, explain_attribution
from mn5_tracker.collectors.sacct import SacctJob
from mn5_tracker.config import AttributionRule, Config, ConfigError, parse_rule
from mn5_tracker.metrics import (
    check_node_spec,
    compute_usage,
    khours_to_node_hours,
    node_hours_to_khours,
    partition_class,
)


def make_job(**overrides: object) -> SacctJob:
    job = SacctJob(
        job_id="1",
        job_name="train_unet_0001",
        account="ehpc103",
        partition="acc",
        qos="acc_ehpc",
        state="COMPLETED",
        state_raw="COMPLETED",
        submit="2026-07-01T10:00:00",
        start="2026-07-01T10:00:00",
        end="2026-07-01T11:00:00",
        elapsed_s=3600,
        n_nodes=1,
        alloc_cpus=160,
        alloc_tres="billing=160,cpu=160,gres/gpu=4,mem=500000M,node=1",
        cpu_time_raw_s=576000,
        exit_code="0:0",
        work_dir="/gpfs/scratch/bsc32/bsc000000/git/geofm",
        consumed_energy_j=None,
    )

    return replace(job, **overrides)


def test_one_acc_node_hour_conversions(config: Config) -> None:
    """1 ACC node × 1 h = 4 GPU-h = 160 billing-h (sacct) = 0.08 khours (bsc_acct)."""
    usage = compute_usage(make_job(), config.node_types)

    assert usage.partition_class == "acc"
    assert usage.node_hours == pytest.approx(1.0)
    assert usage.billed_node_hours == pytest.approx(1.0)
    assert usage.gpu_hours_billed == pytest.approx(4.0)
    assert usage.gpu_hours_node == pytest.approx(4.0)
    assert usage.billing_units_per_node == 160
    assert usage.khours_billed == pytest.approx(0.08)


def test_quarter_node_job_is_billed_a_quarter(config: Config) -> None:
    job = make_job(alloc_cpus=40, alloc_tres="billing=40,cpu=40,gres/gpu=1,node=1")
    usage = compute_usage(job, config.node_types)

    assert usage.node_hours == pytest.approx(1.0)
    assert usage.billed_node_hours == pytest.approx(0.25)
    assert usage.gpu_hours_billed == pytest.approx(1.0)
    assert usage.gpu_hours_requested == pytest.approx(1.0)
    assert usage.gpu_hours_node == pytest.approx(4.0)


def test_gpp_partial_node_and_empty_tres(config: Config) -> None:
    job = make_job(partition="gpp", alloc_cpus=2, alloc_tres="")
    usage = compute_usage(job, config.node_types)

    assert usage.partition_class == "gpp"
    assert usage.billed_node_hours == pytest.approx(2 / 224)
    assert usage.gpu_hours_billed == 0


def test_khours_round_trip(config: Config) -> None:
    acc = config.node_types["acc"]

    assert khours_to_node_hours(271.43, acc) == pytest.approx(3392.875)
    assert node_hours_to_khours(1.0, acc) == pytest.approx(0.08)


def test_partition_class_multi_partition(config: Config) -> None:
    assert partition_class("acc,gpp", config.node_types) == "acc"
    assert partition_class("gpp", config.node_types) == "gpp"
    assert partition_class("nord4", config.node_types) is None


def test_node_spec_check_flags_more_billing_than_threads(config: Config) -> None:
    job = make_job(alloc_tres="billing=320,cpu=320,node=1")
    usage = compute_usage(job, config.node_types)

    assert check_node_spec(job, usage, config.node_types) is not None
    assert (
        check_node_spec(make_job(), compute_usage(make_job(), config.node_types), config.node_types)
        is None
    )


RULES = (
    AttributionRule(index=1, project="geofm", workdir_glob="*/git/geofm*"),
    AttributionRule(index=2, project="hydro", workdir_glob="*/hydro/*"),
    AttributionRule(
        index=3, project="bench", jobname_glob="bench_*", submitted_from=date(2026, 1, 1)
    ),
    AttributionRule(index=4, project="geofm", account_in=("ehpc103",)),
)


def test_first_matching_rule_wins() -> None:
    assert attribute(make_job(), RULES).rule_index == 1

    job = make_job(work_dir="/gpfs/scratch/bsc32/bsc000000/hydro/devel", account="ehpc103")

    assert attribute(job, RULES).project == "hydro"


def test_account_fallback_and_unattributed() -> None:
    elsewhere = make_job(work_dir="/home/bsc/bsc000000")

    assert attribute(elsewhere, RULES).rule_index == 4
    assert attribute(replace(elsewhere, account="bsc32"), RULES).project == UNATTRIBUTED
    assert attribute(replace(elsewhere, account="bsc32"), RULES).rule is None


def test_date_bounded_rule() -> None:
    old = make_job(
        job_name="bench_x", work_dir="/tmp", account="bsc32", submit="2025-06-01T00:00:00"
    )
    new = replace(old, submit="2026-06-01T00:00:00")

    assert attribute(old, RULES).project == UNATTRIBUTED
    assert attribute(new, RULES).project == "bench"


def test_explain_lists_rules_up_to_match() -> None:
    job = make_job(work_dir="/home/bsc/bsc000000")
    evaluations = explain_attribution(job, RULES)

    assert [evaluation.matched for evaluation in evaluations] == [False, False, False, True]
    assert evaluations[0].failed_conditions == ("workdir_glob",)


def test_rules_without_conditions_are_rejected() -> None:
    with pytest.raises(ConfigError, match="no conditions"):
        parse_rule(1, {"project": "x"})

    with pytest.raises(ConfigError, match="unknown keys"):
        parse_rule(1, {"project": "x", "workdir": "*"})
