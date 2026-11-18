"""A standard something is held to, and how one thing measures against it.

One primitive for every standard HQ keeps (HQ's own, a repository's access and
settings, how a container is run), so each check says why it matters and how
to fix it. "Cannot read" is never "not met", and a check a subject's job
requires it to fail, declared with the reason, is "by design" and never a gap.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Self

from .labels import dotted
from .tone import Tone
from .ui import ended
lazy from . import dashboard
lazy from .finding_model import Finding
lazy from .standard_findings import worded


class Standing(StrEnum):
    """Where one check stands: every word a page says of it is a view of this."""

    MET = "met"
    UNMET = "unmet"
    UNAVAILABLE = "unavailable"


# A result, not a standing: a check failed on purpose, for a declared reason.
INTENDED = "intended"

# The word a page answers a check's statement with, by its result's state.
ANSWER = {Standing.MET: "Yes", Standing.UNMET: "No", Standing.UNAVAILABLE: "Unknown", INTENDED: "By design"}


@dataclass(frozen=True, slots=True, kw_only=True)
class Reading:
    """What one check found. Built only by ``met``, ``unmet`` and
    ``unavailable``, so each carries only what its standing can mean."""

    standing: Standing
    evidence: str = ""
    # Met, with a limit the reader should see: a login whose service offers
    # nothing narrower is the best it can be, not a clean pass.
    limited: bool = False
    # What would let HQ read an unavailable check, and where that is done.
    fix: str = ""
    fix_url: str = ""
    # What a reader must hold to be told the evidence: the read gates of the
    # connection families whose instances it names.
    read_capabilities: tuple[str, ...] = ()
    # Why it stands so, in a sentence, kept apart from the evidence so the
    # reasoning and the reading are never confused for each other.
    detail: str = ""
    # The rules behind it, where what decided it had rules.
    rules: tuple[str, ...] = ()

    @classmethod
    def met(cls, evidence: str = "", *, limited: bool = False, detail: str = "", rules: tuple[str, ...] = ()) -> Self:
        return cls(standing=Standing.MET, evidence=evidence, limited=limited, detail=detail, rules=rules)

    @classmethod
    def unmet(
        cls,
        evidence: str = "",
        *,
        read_capabilities: tuple[str, ...] = (),
        detail: str = "",
        rules: tuple[str, ...] = (),
    ) -> Self:
        return cls(
            standing=Standing.UNMET, evidence=evidence, read_capabilities=read_capabilities, detail=detail, rules=rules
        )

    @classmethod
    def unavailable(cls, cause: str, *, fix: str = "", fix_url: str = "", detail: str = "") -> Self:
        return cls(standing=Standing.UNAVAILABLE, evidence=cause, fix=fix, fix_url=fix_url, detail=detail)

    @classmethod
    def judged(cls, holds: bool, evidence: str = "", *, detail: str = "", rules: tuple[str, ...] = ()) -> Self:
        """Met or not, from the one judgment that decides it."""

        return (cls.met if holds else cls.unmet)(evidence, detail=detail, rules=rules)


@dataclass(frozen=True, slots=True)
class Check:
    id: str
    label: str
    test: Callable[[Any], Reading]
    why: str
    fix: str
    # Someone or something gaining reach, rather than a setting drifting.
    serious: bool = False
    # Which group of a standard it belongs to, for a page that groups them.
    scope: str = ""
    # Whether it applies to a given subject; every subject when None.
    applies: Callable[[Any], bool] | None = None
    # What is wrong when it fails, in a clause a list of failures reads as
    # ("vulnerability alerts are off"); the label says what passing is.
    unmet: str = field(kw_only=True)
    # What is true when it is met, as a sentence: a met check's tooltip.
    holds: str = field(default="", kw_only=True)
    # The step of a request's path it is decided at (``path_model.Hop.step``),
    # for a check a request is measured against; HQ's own hop when the path
    # has no such step.
    hop: str = field(default="hq", kw_only=True)
    # The security boundary it belongs to, and what enforces it.
    boundary: str = field(default="", kw_only=True)
    mechanism: str = field(default="", kw_only=True)
    # Its examples, for a check of a standard that has them (``Examples``):
    # changes to the kept record it alone fails (none for a check that is only
    # ever met or not read), and changes it still meets.
    breaks: tuple[Mapping[str, Any], ...] | None = field(default=None, kw_only=True)
    keeps: tuple[Mapping[str, Any], ...] = field(default=(), kw_only=True)

    @property
    def why_said(self) -> str:
        """Why it matters as a sentence, where ``why`` may be a clause of what it stops."""

        if not self.why or self.why.endswith("."):
            return self.why
        return ended(f"Stops {self.why}")


@dataclass(frozen=True, slots=True, kw_only=True)
class Examples:
    """A standard decided from its subject alone, shown at work as every Part
    shows itself in its example: the record of a subject that meets every
    check, those of one nothing was read for, and how a subject is built from
    a record. Each check's ``breaks`` and ``keeps`` change the kept record,
    and one test walks them all."""

    checks: tuple[Check, ...]
    subject: Callable[[Mapping[str, Any]], Any]
    kept: Mapping[str, Any]
    unread: tuple[Mapping[str, Any], ...] = ({},)
    # Whether a subject is offered the fix of a check it fails as it is done
    # (commands, a change to paste) or told why not.
    fixed: Callable[[Any, Check], bool] | None = None

    def of(self, change: Mapping[str, Any]) -> Any:
        """The subject of the kept record with ``change`` laid over it."""

        return self.subject(_laid(self.kept, change))


def _laid(record: Mapping[str, Any], change: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **record,
        **{
            key: _laid(record[key], value)
            if isinstance(value, Mapping) and isinstance(record.get(key), Mapping)
            else value
            for key, value in change.items()
        },
    }


@dataclass(frozen=True, slots=True)
class CheckResult:
    check: Check
    reading: Reading
    # Why it is failed on purpose, for an ``intended`` result.
    reason: str = ""
    # An unmet check is a fault; otherwise an open item, shown quietly.
    enforced: bool = True

    @property
    def state(self) -> str:
        """Its standing, or ``intended`` for a check failed on purpose."""

        return INTENDED if self.reason else self.reading.standing

    @property
    def met(self) -> bool:
        return self.reading.standing is Standing.MET

    @property
    def answer(self) -> str:
        return ANSWER[self.state]

    @property
    def tone(self) -> str:
        if self.met:
            return Tone.NEUTRAL if self.reading.limited else Tone.GOOD
        if self.state == Standing.UNMET and self.enforced:
            return Tone.SERIOUS if self.check.serious else Tone.ATTENTION
        return Tone.NEUTRAL

    @property
    def icon(self) -> str:
        """A tick for met, a cross for a fault, a clock for not read yet: an
        unread check is waiting on a reading, not failing."""

        if self.met:
            return "check-circle"
        if self.reading.standing is Standing.UNAVAILABLE:
            return "clock"
        return "x-circle" if self.tone in {Tone.SERIOUS, Tone.ATTENTION} else "circle"


@dataclass(frozen=True, slots=True)
class Measurement:
    subject: Any
    results: tuple[CheckResult, ...]
    # Whether an unmet check is a fault (Advanced HQ Posture, for HQ's own).
    enforced: bool = True

    @property
    def met(self) -> int:
        return sum(1 for result in self.results if result.state == Standing.MET)

    @property
    def measured(self) -> int:
        return sum(1 for result in self.results if result.state != Standing.UNAVAILABLE)

    @property
    def unread(self) -> int:
        return len(self.results) - self.measured

    @property
    def unmet(self) -> tuple[CheckResult, ...]:
        return tuple(result for result in self.results if result.state == Standing.UNMET)

    @property
    def count_line(self) -> str:
        """ "5 of 7 met · not met: Tailnet lock is on · 1 not read yet": a count
        names what it is short of, and an unread check is not counted as unmet."""

        parts = [f"{self.met} of {len(self.results)} met"]
        if self.unmet:
            parts.append(f"not met: {', '.join(result.check.label for result in self.unmet)}")
        if self.unread:
            parts.append(f"{self.unread} not read yet")
        return dotted(*parts)

    @property
    def summary(self) -> str:
        """How many checks pass, then what is wrong in each that fails: what a
        meter of the results says as one control."""

        passing = f"{self.met} of {self.measured} {'check passes' if self.measured == 1 else 'checks pass'}"
        if not self.unmet:
            return passing
        return f"{passing}; {', '.join(result.check.unmet for result in self.unmet)}"

    @property
    def intended(self) -> tuple[CheckResult, ...]:
        return tuple(result for result in self.results if result.state == INTENDED)

    @property
    def serious(self) -> bool:
        return any(result.check.serious for result in self.unmet)

    @property
    def serious_unmet(self) -> str:
        """What is wrong in each serious check it fails, a line each: what a
        mark that it is serious says on hover."""

        return "\n".join(result.check.unmet for result in self.unmet if result.check.serious)

    @property
    def findings(self) -> tuple[Finding, ...]:
        """What it does not meet, serious first, then the rest, then each it
        fails by design with why."""

        unmet = sorted(self.unmet, key=lambda result: not result.check.serious)
        return tuple(worded(_rule(result.check), (result.check,), [(None, result)]) for result in unmet) + tuple(
            Finding(
                rule=_rule(result.check),
                subject="",
                title=f"{result.check.unmet} by design",
                severity=Tone.GOOD,
                explanation=result.reason,
            )
            for result in self.intended
        )

    @property
    def finding_rows(self) -> tuple[dict[str, Any], ...]:
        """Its findings as the queue's rows, for the page about its subject."""

        return dashboard.finding_rows("standards", "Standards", self.findings)

    def state_of(self, check_id: str) -> str:
        """``met``, ``unmet``, ``intended``, ``unavailable``, or blank for a check that does not apply."""

        return next((result.state for result in self.results if result.check.id == check_id), "")


def measure(
    subject: Any,
    standard: tuple[Check, ...],
    intended: Mapping[str, str] | None = None,
    *,
    enforced: bool = True,
    quiet: tuple[Check, ...] = (),
) -> Measurement:
    """``subject`` against every check in ``standard`` that applies to it.
    ``intended`` maps a check it fails on purpose to why; with ``enforced``
    false an unmet check is an open item rather than a fault, as it is for
    each check in ``quiet``."""

    results = []
    for check in standard:
        if check.applies is not None and not check.applies(subject):
            continue
        reading = check.test(subject)
        reason = (intended or {}).get(check.id, "") if reading.standing is Standing.UNMET else ""
        results.append(CheckResult(check, reading, reason, enforced and not any(check is held for held in quiet)))
    return Measurement(subject, tuple(results), enforced)


def _rule(check: Check) -> str:
    # Its own name, apart from every estate rule's: a check's finding is drawn on its subject's page alone.
    return f"standard.{check.id}"
