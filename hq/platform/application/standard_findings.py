"""How a standard's unmet checks become findings: ``Standard`` raises them on
Needs you and ``worded`` words each, on the queue and its subject's page alike.
Apart from ``standards`` so the checks, which much of HQ imports, load no
finding vocabulary."""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from .finding_model import Finding, FindingEstate, FindingRemedy, FindingRule
from .finding_steps import Do, Run
from .labels import lower_first
from .standards import Check, CheckResult, Measurement
from .tone import Tone
from .workflow_contracts import ActionLink


class Per(StrEnum):
    """One finding per check, naming each subject that fails it, or one per
    subject, folding each check it fails."""

    CHECK = "check"
    SUBJECT = "subject"


@dataclass(frozen=True, slots=True, kw_only=True)
class Measured:
    """One subject of a standard, measured, and how a finding about it names it."""

    measurement: Measurement
    name: str = ""
    node: str = ""
    part: str = ""
    observed_at: datetime | None = None
    reads: str = ""
    # Asked only of a subject a finding is raised about, since what reaches it is costly to derive.
    reach: Callable[[Tone], tuple[Tone, str]] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Help:
    """What a standard's finding offers for one check beyond its fix in words."""

    steps: tuple[Run | Do, ...] = ()
    remedies: tuple[FindingRemedy, ...] = ()
    offers: tuple[ActionLink, ...] = ()
    no_help_reason: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class Standard:
    """A standard whose unmet checks are raised on Needs you, by its own rule."""

    name: str
    title: str
    severity: Tone
    checks: tuple[Check, ...]
    subjects: Callable[[FindingEstate], Iterable[Measured]]
    raises: Per
    operator_action: str
    no_help_reason: str
    bars: dict[str, str]
    primary: Callable[[], ActionLink] | None = None
    helps: Callable[[Check, tuple[Measured, ...]], Help] | None = None

    def rule(self, **declared: Any) -> FindingRule:
        """Its rule, gathered since it reads HQ's records rather than the projection."""

        return FindingRule(
            self.name,
            self.title,
            self.severity,
            self,
            operator_action=self.operator_action,
            no_help_reason=self.no_help_reason,
            bars=self.bars,
            gathered=True,
            **declared,
        )

    def __call__(self, estate: FindingEstate) -> tuple[Finding, ...]:
        return self.raised(self.subjects(estate))

    def raised(self, subjects: Iterable[Measured]) -> tuple[Finding, ...]:
        measured = tuple(subjects)
        unmet = [
            (item, result) for item in measured for result in item.measurement.unmet if result.check in self.checks
        ]
        groups: list[tuple[list[tuple[Measured, CheckResult]], Measured | None]] = (
            [([pair for pair in unmet if pair[1].check is check], None) for check in self.checks]
            if self.raises is Per.CHECK
            else [([pair for pair in unmet if pair[0] is about], about) for about in measured]
        )
        found = []
        for failing, about in groups:
            if failing:
                checks = tuple(result.check for _item, result in failing) if about else (failing[0][1].check,)
                said = [self.helps(check, _subjects(failing)) for check in checks] if self.helps else None
                primary = self.primary() if self.primary else None
                found.append(worded(self.name, checks, failing, about=about, said=said, primary=primary))
        return tuple(found)


def _subjects(failing: Sequence[tuple[Measured | None, CheckResult]]) -> tuple[Measured, ...]:
    return tuple({id(item): item for item, _result in failing if item is not None}.values())


def worded(
    rule: str,
    checks: tuple[Check, ...],
    failing: Sequence[tuple[Measured | None, CheckResult]],
    *,
    about: Measured | None = None,
    said: list[Help] | None = None,
    primary: ActionLink | None = None,
) -> Finding:
    """What is wrong as the title, after the subject's name for a finding
    about one; what reaches it, else why its checks matter, as the
    explanation; where it fails and what was read as the evidence; and each
    fix as a step, with why it matters, unless its help names its own."""

    helped = said or [Help()] * len(checks)
    clauses = [check.unmet for check in checks]
    subjects = _subjects(failing)
    named = [item.name for item in subjects if item.name]
    severity, reached = Tone.SERIOUS if any(check.serious for check in checks) else Tone.ATTENTION, ""
    if about and about.reach:
        severity, reached = about.reach(severity)
    # Why each check matters, where nothing reaches it; a check that says no why leaves its failing.
    whys = " ".join(dict.fromkeys(check.why_said for check in checks if check.why_said))
    explanation = reached or whys or "It does not meet its standard."
    return Finding(
        rule=rule,
        subject=about.node if about else "",
        part=about.part if about else checks[0].id,
        title=f"{about.name}: {', '.join(map(lower_first, clauses))}" if about and about.name else ", ".join(clauses),
        severity=severity,
        explanation=explanation,
        evidence=(
            *((("Not met in", ", ".join(named)),) if named and not about else ()),
            *dict.fromkeys(
                (result.check.label, result.reading.evidence) for _item, result in failing if result.reading.evidence
            ),
        ),
        primary=primary,
        steps=tuple(
            step
            for check, help in zip(checks, helped, strict=True)
            for step in (help.steps or ((Do(check.fix, notes=why(check, explanation)),) if check.fix else ()))
        ),
        remedies=tuple(dict.fromkeys(remedy for help in helped for remedy in help.remedies)),
        offers=tuple(dict.fromkeys(offer for help in helped for offer in help.offers)),
        no_help_reason=next((help.no_help_reason for help in helped if help.no_help_reason), ""),
        read_capabilities=tuple(
            dict.fromkeys(name for _item, result in failing for name in result.reading.read_capabilities)
        ),
        count=0 if about else len(named),
        observed_at=min((item.observed_at for item in subjects if item.observed_at), default=None),
        reads=next((item.reads for item in subjects if item.reads), ""),
    )


def why(check: Check, said: str = "") -> tuple[str, ...]:
    """Why a check matters, as the note under its fix, unless the finding
    already ``said`` it."""

    return (check.why_said,) if check.why_said and check.why_said not in said else ()
