"""Ordered, first-match-wins rules that map a job (allocation) to a scientific project."""

from dataclasses import dataclass
from datetime import date
from fnmatch import fnmatchcase
from typing import Protocol

from mn5_tracker.config import AttributionRule


UNATTRIBUTED = "unattributed"


class AttributableJob(Protocol):
    @property
    def job_name(self) -> str: ...

    @property
    def account(self) -> str: ...

    @property
    def work_dir(self) -> str: ...

    @property
    def submit(self) -> str | None: ...


@dataclass(frozen=True)
class Attribution:
    project: str
    rule: AttributionRule | None

    @property
    def rule_index(self) -> int | None:
        return self.rule.index if self.rule else None


@dataclass(frozen=True)
class RuleEvaluation:
    rule: AttributionRule
    matched: bool
    failed_conditions: tuple[str, ...]


def submit_date(job: AttributableJob) -> date | None:
    return date.fromisoformat(job.submit[:10]) if job.submit else None


def failed_conditions(rule: AttributionRule, job: AttributableJob) -> tuple[str, ...]:
    """Names of the rule's conditions that this job does not satisfy (empty = match)."""
    failures = []
    submitted = submit_date(job)

    if rule.workdir_glob and not fnmatchcase(job.work_dir, rule.workdir_glob):
        failures.append("workdir_glob")

    if rule.jobname_glob and not fnmatchcase(job.job_name, rule.jobname_glob):
        failures.append("jobname_glob")

    if rule.account_in and job.account not in rule.account_in:
        failures.append("account_in")

    if rule.submitted_from and (submitted is None or submitted < rule.submitted_from):
        failures.append("submitted_from")

    if rule.submitted_until and (submitted is None or submitted > rule.submitted_until):
        failures.append("submitted_until")

    return tuple(failures)


def attribute(job: AttributableJob, rules: tuple[AttributionRule, ...]) -> Attribution:
    for rule in rules:
        if not failed_conditions(rule, job):
            return Attribution(project=rule.project, rule=rule)

    return Attribution(project=UNATTRIBUTED, rule=None)


def explain_attribution(
    job: AttributableJob, rules: tuple[AttributionRule, ...]
) -> list[RuleEvaluation]:
    """Evaluate rules in order up to and including the first match, for `mn5track explain`."""
    evaluations = []

    for rule in rules:
        failures = failed_conditions(rule, job)
        evaluations.append(
            RuleEvaluation(rule=rule, matched=not failures, failed_conditions=failures)
        )

        if not failures:
            break

    return evaluations
