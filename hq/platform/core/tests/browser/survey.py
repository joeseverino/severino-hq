"""Every check the layout gates hold, collected from the probe modules that
declare them (`probes/__init__.py` says how), and how a reading becomes
findings."""

import importlib
import pkgutil
from collections.abc import Callable, Mapping
from functools import partial
from typing import Any, override

from hq.platform.core.tests.browser import probes
from hq.platform.core.tests.browser.probes import Check
from hq.platform.core.tests.browser.reading import _probe


def _declared() -> dict[str, Check]:
    """Every probe module's checks, in the order of its name and then its
    declarations, so every run collects one order."""

    found: dict[str, Check] = {}
    for module in sorted(info.name for info in pkgutil.iter_modules(probes.__path__)):
        for check, declared in getattr(importlib.import_module(f"{probes.__name__}.{module}"), "CHECKS", {}).items():
            found[check] = declared if isinstance(declared, Check) else Check(declared)
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


def geometry_probes(path, width):
    """The geometry's probes on ``path`` at ``width``, as ``(source, arg)``
    by check: a check declared for one path only on that path."""

    return Probes(
        {name: _read(check) for name, check in CHECKS.items() if not check.palette and check.only_on in {None, path}}
    )


def table_probes():
    """The table checks' probes, as ``(source, arg)`` by check: those the
    sweep of every width (`BrowserGate.table_defects_at_every_width`) reads."""

    return {name: _read(check)() for name, check in CHECKS.items() if check.table}


def survey_checks(path, width):
    """The checks the survey runs on ``path`` at ``width``, by name: the
    probes' and the fills, which the gate reads in both palettes at once."""

    return [*geometry_probes(path, width), *palette_probes(), "fills"]


def _as_is(value):
    return value


# What turns a probe's value into its check's findings, where that is not the
# value itself.
AS_FINDINGS = {name: check.findings for name, check in CHECKS.items() if check.findings}
