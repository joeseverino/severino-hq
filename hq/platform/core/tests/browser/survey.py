"""Every check the layout gates hold, collected from the probe modules that
declare them (`probes/__init__.py` says how), how a reading becomes
findings, and how a page is opened for them to read what its folds hold."""

import importlib
import pkgutil
from collections.abc import Callable, Mapping
from functools import partial
from typing import Any, override

lazy from playwright.sync_api import Error as BrowserError

from hq.platform.core.tests.browser import probes
from hq.platform.core.tests.browser.probes import Check
from hq.platform.core.tests.browser.reading import _probe


def _declared() -> dict[str, Check]:
    """Every probe module's checks, in the order of its name and then its
    declarations, so every run collects one order."""

    found: dict[str, Check] = {}
    for module in sorted(info.name for info in pkgutil.iter_modules(probes.__path__)):
        found |= getattr(importlib.import_module(f"{probes.__name__}.{module}"), "CHECKS", {})
    return found


CHECKS = _declared()


def _read(check: Check) -> Callable[[], Any]:
    return check.probe if callable(check.probe) else partial(_probe, check.probe)


class Probes(Mapping[str, Any]):
    """Probes as ``(source, arg)`` by check, each read only when it is asked
    for: naming the checks reads none, so a tree without the lint tools (the
    image, the composed suite's copy) names them all the same."""

    def __init__(self, reads: Mapping[str, Callable[[], Any]]):
        self._reads = reads

    @override
    def __getitem__(self, check):
        return self._reads[check]()

    @override
    def __contains__(self, check):
        return check in self._reads

    @override
    def __iter__(self):
        return iter(self._reads)

    @override
    def __len__(self):
        return len(self._reads)


def palette_probes():
    """Every check whose verdict depends on the palette the page is drawn in."""

    return Probes({name: _read(check) for name, check in CHECKS.items() if check.palette})


def geometry_probes(scripted=True):
    """The geometry's probes, as ``(source, arg)`` by check: every check whose
    verdict does not depend on the palette, on a page whose script has run
    when ``scripted``, else every one that holds without it."""

    return Probes(
        {name: _read(check) for name, check in CHECKS.items() if not check.palette and (scripted or not check.scripted)}
    )


def table_probes():
    """The table checks' probes, as ``(source, arg)`` by check: those the
    sweep of every width (`BrowserGate.table_defects_at_every_width`) reads."""

    return {name: _read(check)() for name, check in CHECKS.items() if check.table}


def survey_checks(path, width, scripted=True):
    """The checks the survey runs on ``path`` at ``width``, with script or
    without, by name: the probes' and the fills, which the gate reads in both
    palettes at once."""

    return [*geometry_probes(scripted), *palette_probes(), "fills"]


def _as_is(value):
    return value


# What turns a probe's value into its check's findings, where that is not the
# value itself.
AS_FINDINGS = {name: check.findings for name, check in CHECKS.items() if check.findings}


# A region waiting on its answer: fragment.js marks one busy until then, and a
# live ask is busy for as long as its job runs.
BUSY = "[data-fragment][aria-busy=true]:not([data-ask])"
# Every fold not yet open, opened and marked as the opener's, and how many. A
# fold inside a closed one is opened with it, so one round reaches every fold
# the page holds; the next rounds reach those a region brings in. An exclusive
# group's name is set aside, so every member stands open at once, each laid
# out in its own place as it is when it alone is open.
OPEN_ONCE = """(busy) => {
  // A region already waiting is not one this round brings in.
  document.querySelectorAll(busy).forEach((region) => { region.lookWaiting = true; });
  let opened = 0;
  document.querySelectorAll('details:not([open])').forEach((details) => {
    details.lookOpened = {name: details.getAttribute('name')};
    details.removeAttribute('name');
    details.open = true;
    opened += 1;
  });
  return opened;
}"""
# No region a round of opening brought in is waiting on its answer.
ANSWERED = "(busy) => [...document.querySelectorAll(busy)].every((region) => region.lookWaiting)"
# Every fold the opener opened, closed again and its group's name given back.
CLOSE_OPENED = """() => document.querySelectorAll('details').forEach((details) => {
  if (!details.lookOpened) return;
  if (details.lookOpened.name !== null) details.setAttribute('name', details.lookOpened.name);
  details.open = false;
  delete details.lookOpened;
})"""
# Rounds of opening, each after what the last one brought in has arrived.
OPEN_ROUNDS = 10


def open_every_fold(lens) -> tuple[int, str]:
    """Every fold on ``lens``'s page open, round after round until a round
    finds none closed: how many it opened, and what kept it short of that,
    a region that never answered or rounds run out. ``lens`` is a browser
    test's (``page``, ``settle`` and ``as_a_reader``, a reader's press)."""

    opened = 0
    for _round in range(OPEN_ROUNDS):
        if not (now := lens.as_a_reader(OPEN_ONCE, BUSY)):
            return opened, ""
        opened += now
        lens.settle()
        try:
            lens.page.wait_for_function(ANSWERED, arg=BUSY)
        except BrowserError as waited:
            return opened, f"a region left loading ({str(waited).splitlines()[0]})"
    return opened, f"folds still closed after {OPEN_ROUNDS} rounds"
