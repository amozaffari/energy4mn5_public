from datetime import date, datetime

import pytest
from conftest import between_sentinels, fixture_text

from mn5_tracker.collectors import ParseError, strip_ansi
from mn5_tracker.collectors.associations import parse_associations, parse_groups
from mn5_tracker.collectors.bsc_acct import parse_bsc_acct
from mn5_tracker.collectors.quota import parse_quota
from mn5_tracker.collectors.sacct import SACCT_FIELDS, build_sacct_command, parse_sacct


def jobs_by_id() -> dict:
    return {job.job_id: job for job in parse_sacct(fixture_text("sacct.psv"))}


def test_sacct_parses_every_fixture_line() -> None:
    jobs = parse_sacct(fixture_text("sacct.psv"))

    assert len(jobs) == 13
    assert all(job.account for job in jobs)


def test_sacct_pending_job_has_unknown_times_and_empty_tres() -> None:
    pending = jobs_by_id()["46784789"]

    assert pending.state == "PENDING"
    assert pending.start is None and pending.end is None
    assert pending.alloc_tres == "" and pending.tres == {}
    assert not pending.is_terminal


def test_sacct_running_job_has_no_end() -> None:
    running = jobs_by_id()["46783172"]

    assert running.end is None
    assert running.elapsed_s == 11632


def test_sacct_cancelled_by_uid_is_normalised() -> None:
    cancelled = jobs_by_id()["1748055"]

    assert cancelled.state == "CANCELLED"
    assert cancelled.state_raw == "CANCELLED by 1000"


def test_sacct_none_start_is_treated_as_unknown() -> None:
    assert jobs_by_id()["2361680"].start is None


def test_sacct_multi_partition_string_is_kept() -> None:
    assert jobs_by_id()["46799999"].partition == "acc,gpp"


def test_sacct_energy_is_optional() -> None:
    jobs = jobs_by_id()

    assert jobs["1120989"].consumed_energy_j is not None
    assert jobs["46783172"].consumed_energy_j is None


def test_sacct_wrong_field_count_fails_loudly() -> None:
    line = "|".join(["x"] * (len(SACCT_FIELDS) + 1))

    with pytest.raises(ParseError, match="expected"):
        parse_sacct(line)


def test_sacct_command_is_explicit_and_incremental() -> None:
    command = build_sacct_command("bsc000000", date(2026, 9, 1))

    assert command.startswith("sacct -u bsc000000 -X -n -P -S 2026-09-01 -E now")
    assert "WorkDir%300" in command


def test_bsc_acct_parses_budgets_despite_ansi() -> None:
    report = parse_bsc_acct(between_sentinels(fixture_text("bsc_acct.txt")))

    assert report.accounts == ["bsc32", "ehpc101", "ehpc102", "ehpc103", "ehpc104"]
    assert report.accounting_updated == datetime(2026, 9, 29, 11, 30, 23)

    budget = next(
        row
        for row in report.budgets
        if row.account == "ehpc103" and row.machine == "Marenostrum5 ACC"
    )

    assert budget.total_khours == 5760.0
    assert budget.used_khours == 271.43
    assert budget.used_pct == 5.0
    assert budget.expiration_date == date(2026, 12, 10)
    assert budget.project_title == "GeoFM - Example allocation C"


def test_bsc_acct_undefined_budget_and_expiry() -> None:
    report = parse_bsc_acct(between_sentinels(fixture_text("bsc_acct.txt")))
    bsc32 = [row for row in report.budgets if row.account == "bsc32"]

    assert {row.machine for row in bsc32} >= {"Marenostrum5 GPP", "Nord4", "CTE-AMD"}
    assert all(row.total_khours is None and row.expiration_date is None for row in bsc32)


def test_bsc_acct_long_name_glued_to_machine() -> None:
    report = parse_bsc_acct(between_sentinels(fixture_text("bsc_acct.txt")))
    glued = [
        row
        for row in report.user_usage
        if row.full_name == "Colleague Long Placeholder Name Number"
    ]

    assert {row.machine for row in glued} == {"Nord4", "Marenostrum5 GPP", "Marenostrum5 ACC"}


def test_bsc_acct_own_row() -> None:
    report = parse_bsc_acct(between_sentinels(fixture_text("bsc_acct.txt")))
    own = next(
        row
        for row in report.user_usage
        if row.account == "ehpc103" and row.user == "bsc000000" and row.machine.endswith("ACC")
    )

    assert own.used_khours == 199.63
    assert own.pct == 74.0


def test_bsc_acct_utf8_names() -> None:
    text = (
        "CPU GROUP BUDGET:\nProject: Test (ehpc1)\nProject's Expiration Date:     2027-01-01\n"
        "Marenostrum5 ACC            10.00 (0.00,0.00)            1.00 (10%)\n"
        "USER CONSUMED CPU:\n"
        "ugen1 (Éamon Ó Catháin)               Marenostrum5 ACC                1.00(100%)\n"
    )

    assert parse_bsc_acct(text).user_usage[0].full_name == "Éamon Ó Catháin"


def test_bsc_acct_unknown_line_fails_loudly() -> None:
    text = "CPU GROUP BUDGET:\nProject: Test (ehpc1)\nsomething new and unexpected\n"

    with pytest.raises(ParseError, match="unrecognised"):
        parse_bsc_acct(text)


def test_strip_ansi_removes_colours_and_hyperlinks() -> None:
    text = "\x1b[1mProject: \x1b[0mX \x1b]8;;https://bsc.es\x1b\\link\x1b]8;;\x1b\\"

    assert strip_ansi(text) == "Project: X link"


def test_associations_and_groups() -> None:
    associations = parse_associations(between_sentinels(fixture_text("sacctmgr_assoc.txt")))

    assert [row.account for row in associations] == [
        "bsc32",
        "ehpc101",
        "ehpc102",
        "ehpc103",
        "ehpc104",
    ]
    assert "acc_ehpc" in associations[1].qos
    assert parse_groups("bsc earth bsc32\n") == ["bsc", "earth", "bsc32"]


def test_quota_rows_per_group() -> None:
    rows = parse_quota(between_sentinels(fixture_text("bsc_quota.txt")))
    scratch = next(
        row for row in rows if row.group == "ehpc103" and row.filesystem == "gpfs_scratch"
    )

    assert {row.group for row in rows} == {"bsc32", "ehpc101", "ehpc102", "ehpc103", "ehpc104"}
    assert scratch.quota_bytes == round(39.06 * 1024**4)
