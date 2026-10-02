"""Replace every name that ties a dashboard to a person, project or allocation with an alias.

Hours, dates and outcomes are kept. Aliases follow config order, so "Project A" or "Allocation 2"
means the same thing in the own view and in every project view generated from the same config.
"""

import json
import re
import string
from dataclasses import dataclass
from typing import Any

from mn5_tracker.attribution import UNATTRIBUTED
from mn5_tracker.config import Config


ANONYMOUS_USER = "Anonymous user"
ANONYMOUS_MEMBER = "Member"

# Slurm account and MN5 username shapes, for names that are not in the config (e.g. an
# allocation only seen in a contribution file).
USERNAME_PATTERN = re.compile(r"\b[a-z]{2,5}\d{5,6}\b", re.IGNORECASE)
ACCOUNT_PATTERN = re.compile(r"\b(?:ehpc\d+|bsc\d{1,3}|cns\d+|res\d+)\b", re.IGNORECASE)


class AnonymisationError(RuntimeError):
    """An identifying name survived anonymisation; the page must not be written."""


@dataclass(frozen=True)
class AliasMap:
    """Original name → alias; every original is a name that must not appear in the output."""

    aliases: dict[str, str]

    @property
    def sensitive(self) -> list[str]:
        return sorted(self.aliases, key=len, reverse=True)

    def pattern(self) -> re.Pattern[str]:
        alternatives = "|".join(re.escape(name) for name in self.sensitive)

        return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE)


def letter_alias(index: int) -> str:
    """A, B, ... Z, then 27, 28, ... for configs with more projects than letters."""
    letters = string.ascii_uppercase

    return letters[index] if index < len(letters) else str(index + 1)


def is_placeholder_title(title: str) -> bool:
    return title.lower().startswith("unknown")


def build_alias_map(config: Config, extra_accounts: set[str], extra_users: set[str]) -> AliasMap:
    aliases: dict[str, str] = {}

    for index, name in enumerate(config.projects):
        alias = f"Project {letter_alias(index)}"
        aliases[name] = alias
        aliases[config.projects[name].title] = alias

    accounts = list(config.allocations) + sorted(extra_accounts - set(config.allocations))

    for index, account in enumerate(accounts, 1):
        alias = f"Allocation {index}"
        aliases[account] = alias
        allocation = config.allocations.get(account)

        if allocation and not is_placeholder_title(allocation.title):
            aliases[allocation.title] = f"{alias} title"

    aliases[config.user] = ANONYMOUS_USER

    for index, user in enumerate(sorted(extra_users - {config.user}), 1):
        aliases[user] = f"{ANONYMOUS_MEMBER} {index}"

    aliases.pop(UNATTRIBUTED, None)

    return AliasMap({name: alias for name, alias in aliases.items() if name})


def names_in_data(data: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Accounts and contributor usernames the data mentions, including ones not in the config."""
    accounts = set(data["series"]["account"]) | {panel["account"] for panel in data["allocations"]}
    coverage = data["scope"].get("coverage") or {}
    accounts |= {item["account"] for item in coverage.get("invisible_allocations", [])}
    users = set(coverage.get("contributors", []))

    return accounts, users


def replace_names(value: Any, pattern: re.Pattern[str], lookup: dict[str, str]) -> Any:
    """Rewrite every string, dict key included, so chart series and their labels stay aligned."""
    if isinstance(value, str):
        renamed = pattern.sub(lambda match: lookup[match.group(0).lower()], value)
        renamed = USERNAME_PATTERN.sub(ANONYMOUS_MEMBER.lower(), renamed)

        return ACCOUNT_PATTERN.sub("another allocation", renamed)

    if isinstance(value, dict):
        return {
            replace_names(key, pattern, lookup): replace_names(item, pattern, lookup)
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [replace_names(item, pattern, lookup) for item in value]

    return value


def assert_anonymous(data: dict[str, Any], alias_map: AliasMap) -> None:
    text = json.dumps(data, ensure_ascii=False)
    leaked = [
        name
        for name in alias_map.sensitive
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE)
    ]
    leaked += USERNAME_PATTERN.findall(text) + ACCOUNT_PATTERN.findall(text)

    if leaked:
        raise AnonymisationError(f"identifying names survived anonymisation: {leaked}")


def anonymise_dashboard(data: dict[str, Any], config: Config) -> dict[str, Any]:
    accounts, users = names_in_data(data)
    alias_map = build_alias_map(config, accounts, users)
    lookup = {name.lower(): alias for name, alias in alias_map.aliases.items()}
    anonymised = replace_names(data, alias_map.pattern(), lookup)

    for panel in anonymised["allocations"]:
        panel["title"] = f"{panel['node_class'].upper()} budget"

    anonymised["scope"]["anonymised"] = True
    anonymised["notes"] = [
        *anonymised["notes"],
        "Anonymised: projects, allocations and people are replaced by aliases; hours are real.",
    ]
    assert_anonymous(anonymised, alias_map)

    return anonymised
