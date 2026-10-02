import json
import re
from datetime import date

import pytest
from conftest import FIXTURE_USER, command_outputs

from mn5_tracker.config import Config
from mn5_tracker.reports.anonymise import (
    AnonymisationError,
    anonymise_dashboard,
    assert_anonymous,
    build_alias_map,
)
from mn5_tracker.reports.dashboard import build_dashboard_data, render_dashboard
from mn5_tracker.store import Store
from mn5_tracker.sync import ingest_outputs


TODAY = date(2026, 9, 29)


def identifying_names(config: Config) -> set[str]:
    names = {FIXTURE_USER, *config.allocations, *config.projects}
    names |= {project.title for project in config.projects.values()}

    return {name for name in names if name}


def contains_name(text: str, name: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE) is not None


@pytest.mark.parametrize("project", [None, "geofm"])
def test_anonymised_dashboard_has_no_identifying_names(
    config: Config, store: Store, project: str | None
) -> None:
    ingest_outputs(config, store, "2026-09-29T09:00:00+00:00", command_outputs())
    data = build_dashboard_data(config, store, TODAY, project)
    html = render_dashboard(anonymise_dashboard(data, config))

    leaked = [name for name in identifying_names(config) if contains_name(html, name)]

    assert leaked == []
    assert not re.search(r"bsc9\d{5}", html)


def test_anonymising_keeps_hours_and_series_aligned(config: Config, store: Store) -> None:
    ingest_outputs(config, store, "2026-09-29T09:00:00+00:00", command_outputs())
    data = build_dashboard_data(config, store, TODAY)
    anonymised = anonymise_dashboard(data, config)

    assert anonymised["tiles"] == data["tiles"]
    assert anonymised["outcomes"] == data["outcomes"]

    for row in anonymised["monthly"]["account"]:
        assert set(row["values"]) <= set(anonymised["series"]["account"])


def test_aliases_are_the_same_in_every_view(config: Config) -> None:
    own = build_alias_map(config, set(), set()).aliases
    project = build_alias_map(config, {"ehpc999"}, {"bsc900001"}).aliases

    assert {name: own[name] for name in own} == {name: project[name] for name in own}
    assert project["ehpc999"].startswith("Allocation ")
    assert project["bsc900001"] == "Member 1"


def test_leftover_name_refuses_to_write(config: Config) -> None:
    alias_map = build_alias_map(config, set(), set())
    first_project = next(iter(config.projects))

    with pytest.raises(AnonymisationError, match=first_project):
        assert_anonymous({"note": f"used by {first_project}"}, alias_map)

    with pytest.raises(AnonymisationError):
        assert_anonymous({"note": json.dumps({"account": "ehpc123"})}, alias_map)
