"""A standard something is held to, and how one thing measures against it.

One primitive for every standard HQ keeps (a repository's access and settings,
how a container is run), so each check says why it matters and how to fix it.
"Cannot read" is never "not met", and a check a subject's job requires it to
fail, declared with the reason, is "by design" and never a gap.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

MET = "met"
UNMET = "unmet"
UNAVAILABLE = "unavailable"
INTENDED = "intended"


@dataclass(frozen=True, slots=True)
class Check:
    id: str
    label: str
    # True (met), False (not met) or None (HQ cannot read it).
    test: Callable[[Any], bool | None]
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


@dataclass(frozen=True, slots=True)
class CheckResult:
    check: Check
    state: str
    # Why it is failed on purpose, for an ``intended`` result.
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Measurement:
    subject: Any
    results: tuple[CheckResult, ...]

    @property
    def met(self) -> int:
        return sum(1 for result in self.results if result.state == MET)

    @property
    def measured(self) -> int:
        return sum(1 for result in self.results if result.state != UNAVAILABLE)

    @property
    def unread(self) -> int:
        return len(self.results) - self.measured

    @property
    def unmet(self) -> tuple[CheckResult, ...]:
        return tuple(result for result in self.results if result.state == UNMET)

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

    def state_of(self, check_id: str) -> str:
        """``met``, ``unmet``, ``intended``, ``unavailable``, or blank for a check that does not apply."""

        return next((result.state for result in self.results if result.check.id == check_id), "")


def measure(subject: Any, standard: tuple[Check, ...], intended: Mapping[str, str] | None = None) -> Measurement:
    """``subject`` against every check in ``standard`` that applies to it.
    ``intended`` maps a check it fails on purpose to why."""

    results = []
    for check in standard:
        if check.applies is not None and not check.applies(subject):
            continue
        outcome = check.test(subject)
        if outcome is None:
            results.append(CheckResult(check, UNAVAILABLE))
        elif outcome:
            results.append(CheckResult(check, MET))
        elif intended and check.id in intended:
            results.append(CheckResult(check, INTENDED, intended[check.id]))
        else:
            results.append(CheckResult(check, UNMET))
    return Measurement(subject, tuple(results))
