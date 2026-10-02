"""Parsers for the stock MN5 commands the tracker reads (sacct, bsc_acct, sacctmgr, bsc_quota)."""

import re


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x1b\x07]*(?:\x1b\\|\x07)")


class ParseError(ValueError):
    """Raised when MN5 output no longer has the structure the parsers were written against."""


def strip_ansi(text: str) -> str:
    """Remove colour codes and OSC-8 hyperlinks that bsc_acct and bsc_quota emit."""
    return ANSI_ESCAPE.sub("", text)
