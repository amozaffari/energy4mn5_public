"""Parse `bsc_quota`: storage usage and limits per Unix group and GPFS filesystem."""

import re
from dataclasses import dataclass

from mn5_tracker.collectors import ParseError, strip_ansi


QUOTA_COMMAND = "bsc_quota"

GROUP_HEADER = re.compile(r"Printing quota for group (?P<group>\S+?):")
SIZE = r"[\d.]+\s+[KMGTP]?B"
QUOTA_ROW = re.compile(
    rf"^(?P<filesystem>gpfs_\w+)\s+(?P<type>USR|GRP|FILESET)\s+"
    rf"(?P<usage>{SIZE})\s+(?P<quota>{SIZE})\s+(?P<limit>{SIZE})\s+(?P<in_doubt>{SIZE})\s+"
    rf"(?P<grace>.+?)\s+\|\s+(?P<files>\d+)\s+(?P<files_in_doubt>\d+)\s*$"
)
UNIT_BYTES = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4, "PB": 1024**5}


@dataclass(frozen=True)
class QuotaRow:
    group: str
    filesystem: str
    quota_type: str
    usage_bytes: int
    quota_bytes: int
    limit_bytes: int
    files: int
    grace: str


def parse_size(value: str) -> int:
    number, unit = value.split()

    return round(float(number) * UNIT_BYTES[unit])


def parse_quota(text: str) -> list[QuotaRow]:
    rows = []
    group = None

    for raw_line in strip_ansi(text).splitlines():
        line = raw_line.strip()

        if match := GROUP_HEADER.search(line):
            group = match["group"]
            continue

        match = QUOTA_ROW.match(line)

        if not match:
            continue

        if group is None:
            raise ParseError(f"bsc_quota: filesystem row before any group header: {line!r}")

        rows.append(
            QuotaRow(
                group=group,
                filesystem=match["filesystem"],
                quota_type=match["type"],
                usage_bytes=parse_size(match["usage"]),
                quota_bytes=parse_size(match["quota"]),
                limit_bytes=parse_size(match["limit"]),
                files=int(match["files"]),
                grace=match["grace"].strip(),
            )
        )

    if "Printing quota for group" in text and not rows:
        raise ParseError("bsc_quota output has group headers but no parseable filesystem rows")

    return rows
