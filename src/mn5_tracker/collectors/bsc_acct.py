"""Parse `bsc_acct`: per-group budgets and every member's consumption, in thousand core-hours."""

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from mn5_tracker.collectors import ParseError, strip_ansi


BSC_ACCT_COMMAND = "/apps/modules/bsc/bin/bsc_acct"

PROJECT_LINE = re.compile(r"^Project:\s*(?P<title>.*?)\s*\((?P<account>[^()\s]+)\)\s*$")
EXPIRATION_LINE = re.compile(r"^Project's Expiration Date:\s*(?P<value>\S+)\s*$")
BUDGET_ROW = re.compile(
    r"^(?P<machine>\S.*?)\s{2,}"
    r"(?:undefined|(?P<total>[\d.]+)\s*\((?P<class_a>[\d.]+),(?P<class_b>[\d.]+)\))\s+"
    r"(?P<used>[\d.]+)(?:\s*\((?P<pct>\d+)%\))?\s*$"
)
USER_ROW = re.compile(
    r"^(?P<user>\S+)\s+\((?P<full_name>[^)]*)\)\s*(?P<machine>\S.*?)\s+"
    r"(?P<used>[\d.]+)\((?P<pct>\d+)%\)\s*$"
)
ACCOUNTING_UPDATED_LINE = re.compile(r"^Accounting updated on\s+(?P<value>.+?)\s*$")
SEPARATOR_LINE = re.compile(r"^-{10,}$")
IGNORED_HEADERS = ("Machine:", "User:")


@dataclass(frozen=True)
class BudgetRow:
    account: str
    project_title: str
    machine: str
    total_khours: float | None
    used_khours: float
    used_pct: float | None
    expiration_date: date | None


@dataclass(frozen=True)
class UserUsageRow:
    account: str
    user: str
    full_name: str
    machine: str
    used_khours: float
    pct: float


@dataclass
class BscAcctReport:
    budgets: list[BudgetRow] = field(default_factory=list)
    user_usage: list[UserUsageRow] = field(default_factory=list)
    accounting_updated: datetime | None = None

    @property
    def accounts(self) -> list[str]:
        return sorted({budget.account for budget in self.budgets})


@dataclass
class GroupContext:
    account: str
    project_title: str
    expiration_date: date | None = None


def parse_expiration(value: str) -> date | None:
    return None if value == "undefined" else date.fromisoformat(value)


def parse_bsc_acct(text: str) -> BscAcctReport:
    """Walk the output as a state machine; any line that fits no known shape is an error."""
    report = BscAcctReport()
    group: GroupContext | None = None
    section: str | None = None

    for line_number, raw_line in enumerate(strip_ansi(text).splitlines(), 1):
        line = raw_line.strip()

        if not line or SEPARATOR_LINE.match(line) or line.startswith(IGNORED_HEADERS):
            continue

        if line == "CPU GROUP BUDGET:":
            section = "budget"
            group = None
            continue

        if line == "USER CONSUMED CPU:":
            section = "users"
            continue

        if match := ACCOUNTING_UPDATED_LINE.match(line):
            report.accounting_updated = datetime.fromisoformat(match["value"])
            continue

        if match := PROJECT_LINE.match(line):
            group = GroupContext(account=match["account"], project_title=match["title"])
            continue

        if match := EXPIRATION_LINE.match(line):
            if group is None:
                raise ParseError(f"bsc_acct line {line_number}: expiration before any project")

            group.expiration_date = parse_expiration(match["value"])
            continue

        if group is None:
            raise ParseError(f"bsc_acct line {line_number}: data outside a project: {line!r}")

        if section == "budget" and (match := BUDGET_ROW.match(line)):
            report.budgets.append(budget_row_from_match(match, group))
            continue

        if section == "users" and (match := USER_ROW.match(line)):
            report.user_usage.append(user_row_from_match(match, group))
            continue

        raise ParseError(f"bsc_acct line {line_number}: unrecognised {section} line: {line!r}")

    if "Project:" in text and not report.budgets:
        raise ParseError("bsc_acct output mentions projects but no budget rows were parsed")

    return report


def budget_row_from_match(match: re.Match[str], group: GroupContext) -> BudgetRow:
    total = match["total"]
    pct = match["pct"]

    return BudgetRow(
        account=group.account,
        project_title=group.project_title,
        machine=match["machine"].strip(),
        total_khours=float(total) if total is not None else None,
        used_khours=float(match["used"]),
        used_pct=float(pct) if pct is not None else None,
        expiration_date=group.expiration_date,
    )


def user_row_from_match(match: re.Match[str], group: GroupContext) -> UserUsageRow:
    return UserUsageRow(
        account=group.account,
        user=match["user"],
        full_name=match["full_name"],
        machine=match["machine"].strip(),
        used_khours=float(match["used"]),
        pct=float(match["pct"]),
    )
