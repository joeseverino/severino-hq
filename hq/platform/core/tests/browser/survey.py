"""Every check the layout gates hold, collected from the probe modules that
declare them (`probes/__init__.py` says how), how a reading becomes
findings, and how a page is opened for them to read what its folds hold."""

import importlib
import inspect
import pkgutil
import re
from collections.abc import Callable, Mapping
from functools import cache, partial
from pathlib import Path
from typing import Any, override

from django.conf import settings
from django.utils.text import slugify
lazy from playwright.sync_api import Error as BrowserError

from hq.platform.core.template_contract import parts
from hq.platform.core.template_parts import Part
from hq.platform.core.tests.browser import probes
from hq.platform.core.tests.browser.probes import Check, Example
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


def examples() -> dict[str, tuple[str, Example, Any]]:
    """Every check's examples (`Check.plants`, `Check.clear`) by the page each
    is drawn on, each with its check and its probe as ``(source, arg)``."""

    return {
        f"{kind}/{slugify(name)}/{at}": (name, example, _read(check)())
        for name, check in CHECKS.items()
        for kind, drawn in (("plant", check.plants), ("clear", check.clear))
        for at, example in enumerate(drawn, 1)
    }


def survey_checks(path, width, scripted=True):
    """The checks the survey runs on ``path`` at ``width``, with script or
    without, by name: the probes' and the fills, which the gate reads in both
    palettes at once."""

    return [*geometry_probes(scripted), *palette_probes(), "fills"]


def findings_of(check, returned, value, drawn=None, on=""):
    """What ``check``'s probe returned or threw as its findings on the page
    ``on``, which draws the part ``drawn`` (its catalog entry's): a probe that
    threw is a finding of its own check, so it hides no other, and one that
    measures a part (`Check.part`) and read none of it on the page that draws
    it has measured nothing, which is a finding too."""

    if not returned:
        return [f"the probe threw: {value}"]
    declared = CHECKS.get(check)
    if declared is None or declared.part is None:
        return declared.findings(value) if declared is not None and declared.findings else value
    if not isinstance(value, dict) or not {"read", "found"} <= value.keys():
        return [f"its probe names a part, so says how much of it it read as {{read, found}}, not {value!r}"]
    found = list(value["found"])
    if drawn is declared.part and not value["read"]:
        found.append(f"measured nothing on {on}, which draws {declared.part.name or declared.part.partial}")
    return found


# A class an element is named by in a finding (`probes/describe.py`'s `describe`).
_NAMED_CLASS = re.compile(r"\.(-?[_a-zA-Z][\w-]*)")


@cache
def _owners() -> dict[str, Part]:
    """Each class a Part writes as its own, by the one Part that owns it
    (`template_contract.parts` refuses two owning one)."""

    return {word: part for part in parts() for word in part.owned()}


def _owner(words: list[str]) -> Part | None:
    """The Part that owns one of ``words`` by its name, else by a prefix
    (`is-*`), so a prefix never takes a class another Part names."""

    owners = _owners()
    named = (owners.get(word) for word in words)
    prefixed = (
        part for word in words for owned, part in owners.items() if owned.endswith("*") and word.startswith(owned[:-1])
    )
    return next(filter(None, named), None) or next(prefixed, None)


def drawn_by(check: str, said: str) -> tuple[str, str]:
    """The Part that draws what a finding of ``check`` names, and the file
    that draws it, the one place to fix: the Part the check measures, else
    the owner of the first class the finding names that one Part owns; empty
    for markup no Part owns."""

    declared = CHECKS.get(check.partition(", with ")[0])
    part = declared.part if declared is not None and declared.part is not None else None
    part = part or _owner(_NAMED_CLASS.findall(said))
    if part is None:
        return "", ""
    if part.partial:
        return part.name or part.partial, f"templates/{part.files()[0]}"
    source = inspect.getsourcefile(part.function) if part.function else None
    return part.name, str(Path(source).relative_to(settings.BASE_DIR)) if source else ""


# A region waiting on its answer: fragment.js marks one busy until then, and a
# live ask is busy for as long as its job runs.
BUSY = "[data-fragment][aria-busy=true]:not([data-ask])"
# Every fold not yet open, opened and marked as the opener's, and how many. A
# fold inside a closed one is opened with it, so one round reaches every fold
# the page holds; the next rounds reach those a region brings in. An exclusive
# group's name is set aside, so every member stands open at once, each laid
# out in its own place as it is when it alone is open. Every row of a table
# that offers its toggle is opened by it, as a reader opens it once script runs.
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
  // A row's toggle opens it only once script runs.
  if (!matchMedia('(scripting: enabled)').matches) return opened;
  document.querySelectorAll('main table [data-row-toggle][aria-expanded="false"]').forEach((toggle) => {
    if (!toggle.checkVisibility()) return;
    toggle.lookOpened = true;
    toggle.click();
    opened += 1;
  });
  return opened;
}"""
# No region a round of opening brought in is waiting on its answer.
ANSWERED = "(busy) => [...document.querySelectorAll(busy)].every((region) => region.lookWaiting)"
# Every fold the opener opened, closed again and its group's name given back.
CLOSE_OPENED = """() => {
  document.querySelectorAll('main table [data-row-toggle]').forEach((toggle) => {
    if (!toggle.lookOpened) return;
    toggle.click();
    delete toggle.lookOpened;
  });
  document.querySelectorAll('details').forEach((details) => {
    if (!details.lookOpened) return;
    if (details.lookOpened.name !== null) details.setAttribute('name', details.lookOpened.name);
    details.open = false;
    delete details.lookOpened;
  });
}"""
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
