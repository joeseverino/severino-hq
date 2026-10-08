"""A standard something is held to, and how one thing measures against it.

One primitive for every standard HQ keeps (a repository's access and settings,
how a container is run): each check says why it matters and how to fix it,
and whether it applies to a given subject. A check answers True (met), False
(not met) or None (HQ cannot read it), and "cannot read" is never "not met".
A subject may declare a check its job requires it to fail, with the reason:
that check is "by design", shown with the reason and never a gap.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

MET = "met"
UNMET = "unmet"
UNAVAILABLE = "unavailable"
INTENDED = "intended"


@dataclass(frozen=True)
class Check:
    id: str
    label: str
    test: Callable[[Any], bool | None]
    why: str
    fix: str
    # Someone or something gaining reach, rather than a setting drifting.
    serious: bool = False
    # Which group of a standard it belongs to, for a page that groups them.
    scope: str = ""
    # Whether it applies to a given subject; every subject when None.
    applies: Callable[[Any], bool] | None = None


@dataclass(frozen=True)
class Result:
    check: Check
    state: str
    # Why it is failed on purpose, for an ``intended`` result.
    reason: str = ""


@dataclass(frozen=True)
class Posture:
    subject: Any
    results: tuple[Result, ...]

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
    def unmet(self) -> tuple[Result, ...]:
        return tuple(result for result in self.results if result.state == UNMET)

    @property
    def intended(self) -> tuple[Result, ...]:
        return tuple(result for result in self.results if result.state == INTENDED)

    @property
    def serious(self) -> bool:
        return any(result.check.serious for result in self.unmet)

    def state_of(self, check_id: str) -> str:
        """``met``, ``unmet``, ``intended``, ``unavailable``, or blank for a check that does not apply."""

        return next((result.state for result in self.results if result.check.id == check_id), "")


def measure(subject: Any, standard: tuple[Check, ...], intended: Mapping[str, str] | None = None) -> Posture:
    """``subject`` against every check in ``standard`` that applies to it.
    ``intended`` maps a check it fails on purpose to why."""

    results = []
    for check in standard:
        if check.applies is not None and not check.applies(subject):
            continue
        outcome = check.test(subject)
        if outcome is None:
            results.append(Result(check, UNAVAILABLE))
        elif outcome:
            results.append(Result(check, MET))
        elif intended and check.id in intended:
            results.append(Result(check, INTENDED, intended[check.id]))
        else:
            results.append(Result(check, UNMET))
    return Posture(subject, tuple(results))
