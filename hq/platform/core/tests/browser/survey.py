"""Every check the layout gates hold, as probes with their arguments, and how a reading becomes findings."""

from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path
from typing import Any, override

from hq.platform.core.tests.browser.probes.axe import contrast_read, rules_read
from hq.platform.core.tests.browser.probes.boxes import (
    CRAMPED_HEADS,
    HEAD_CONTROLS,
    INTO_A_NEIGHBOR,
    OVERLAPS,
    SIDEWAYS,
    SMALL_TARGETS,
)
from hq.platform.core.tests.browser.probes.palette import BUTTON_EDGES, FIELD_CONTRAST, FIELD_EDGES
from hq.platform.core.tests.browser.probes.parts import (
    DIALOGS_WITHOUT_OPENERS,
    HIDDEN_IN_SUMMARIES,
    PARTS_NOT_UNDERLINED,
    READINGS,
    SELECT_ARROWS,
    SPILLED_LABELS,
    STRANDED_EVIDENCE,
    WRAPPED_BESIDE_A_PAIR,
    reading_problems,
)
from hq.platform.core.tests.browser.probes.shifts import CLS_LIMIT, LAYOUT_SHIFTS
from hq.platform.core.tests.browser.probes.tables import (
    CELLS,
    CUT_AND_CLOSED,
    CUT_WHEN_OPEN,
    CUT_WITH_ROOM,
    PAST_THE_GUTTER,
    TABLES,
    TABLES_THAT_DO_NOT_FIT,
    UNSEEN_COLUMNS,
)
from hq.platform.core.tests.browser.probes.text import (
    BROKEN_TOKENS,
    BROKEN_WORDS,
    BROKEN_WORDS_IN_TEXT,
    CHANGES_OFF_THEIR_LINE,
    CHIPS_OFF_THEIR_LINE,
    CUT_DECISIONS,
    OVERLAPPING_TEXT,
    PAST_THE_CLAMP,
    RAISED_TEXT,
)
from hq.platform.core.tests.browser.reading import _probe
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.widths import TARGET_MIN

AUDIT = Path(__file__).parents[2] / "browser_audit.js"

# The rule from browser_audit.js that finds what a reader loses and no other
# check does: a scrollbar no one asked for. Content clipped out of sight is
# the sideways check's; the other rules judge density and evenness, which
# dev:look shows by eye rather than the gate.
AUDIT_RULES = {"unwanted-scrollbar"}

_AUDIT = (
    "async () => { const audit = "
    + AUDIT.read_text(encoding="utf-8").strip()
    + ";\nreturn await audit({ evaluate: (probe) => probe() }); }"
)

# What is an identifier rather than a word, which the broken-word rule leaves
# to break where it must.
_IDENTIFIER = "code, kbd, samp, time, .token, [data-entity]"

_FIELDS_READ = (FIELD_EDGES, [SELECTORS["fillable"], FIELD_CONTRAST, SELECTORS["pressed"]])

_BUTTON_EDGES_READ = (BUTTON_EDGES, SELECTORS["pressed"])


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


_PALETTE: dict[str, Callable[[], Any]] = {
    "contrast": contrast_read,
    "fields": partial(_probe, _FIELDS_READ),
    "button edges": partial(_probe, _BUTTON_EDGES_READ),
}


def palette_probes():
    """Every check whose verdict depends on the palette the page is drawn in."""

    return Probes(_PALETTE)


_SIDEWAYS_READ = (SIDEWAYS, SELECTORS["sideways"])

_SPILLED_CONTROLS = (SPILLED_LABELS, SELECTORS["control"])

_SPILLED_CELLS = (SPILLED_LABELS, SELECTORS["cell_content"])


EVIDENCE_READ = (STRANDED_EVIDENCE, [SELECTORS["check_statement"], SELECTORS["check_evidence"]])

SUMMARIES_READ = (HIDDEN_IN_SUMMARIES, SELECTORS["summary_value"])


_SMALL_TARGETS_READ = (SMALL_TARGETS, [SELECTORS["target"], TARGET_MIN, SELECTORS["floating"]])

CHANGES_READ = (
    CHANGES_OFF_THEIR_LINE,
    [SELECTORS["change"], SELECTORS["change_arrow"], SELECTORS["change_to"], SELECTORS["unseen"]],
)

CHIPS_READ = (CHIPS_OFF_THEIR_LINE, [SELECTORS["chip"], SELECTORS["unseen"]])

HEADS_READ = (
    HEAD_CONTROLS,
    [SELECTORS["head_actions"], SELECTORS["head_menu"], SELECTORS["head_menu_panel"], SELECTORS["ask"]],
)


_READINGS_OF = f"(selector) => ({READINGS})([...document.querySelectorAll(selector)])"

# Every probe the layout invariants read on a page, by the check it answers.
_GEOMETRY = {
    "sideways": _SIDEWAYS_READ,
    "cells": CELLS,
    "broken words": BROKEN_WORDS,
    "words broken in text": (BROKEN_WORDS_IN_TEXT, _IDENTIFIER),
    "broken identifiers": (BROKEN_TOKENS, [SELECTORS["cell_token"], SELECTORS["wraps_in_a_cell"]]),
    "overlaps": OVERLAPS,
    "into a neighbor": INTO_A_NEIGHBOR,
    "overlapping text": (OVERLAPPING_TEXT, SELECTORS["floating"]),
    "tables": TABLES,
    "tables fit": (TABLES_THAT_DO_NOT_FIT, SELECTORS["table_scroll"]),
    "unseen columns": (UNSEEN_COLUMNS, SELECTORS["table_scroll"]),
    "control labels": _SPILLED_CONTROLS,
    "cell labels": _SPILLED_CELLS,
    "small targets": _SMALL_TARGETS_READ,
    "past the clamp": PAST_THE_CLAMP,
    "cut decisions": (CUT_DECISIONS, [SELECTORS["decision_whole"], "*", SELECTORS["unseen"]]),
    "cut code in text": (CUT_DECISIONS, [SELECTORS["running_text"], SELECTORS["code"], SELECTORS["unseen"]]),
    "cut with room": (CUT_WITH_ROOM, [SELECTORS["cut_box"], SELECTORS["name_box"], SELECTORS["unseen"]]),
    "cut and closed": (CUT_AND_CLOSED, SELECTORS["unseen"]),
    "cut when open": (CUT_WHEN_OPEN, SELECTORS["unseen"]),
    "changes": CHANGES_READ,
    "chips": CHIPS_READ,
    "raised text": (RAISED_TEXT, SELECTORS["unseen"]),
    "heads": HEADS_READ,
    "section heads": (CRAMPED_HEADS, SELECTORS["section_head"]),
    "dialogs": (DIALOGS_WITHOUT_OPENERS, [SELECTORS["dialog"], SELECTORS["dialog_opener"]]),
    "select arrows": (SELECT_ARROWS, [SELECTORS["dropdown"], SELECTORS["vendored"]]),
    "link underlines": (PARTS_NOT_UNDERLINED, [SELECTORS["link"], SELECTORS["vendored"]]),
    "lists of facts": (WRAPPED_BESIDE_A_PAIR, SELECTORS["fact_list"]),
    # Every box and every line of text, not only a card's or code's.
    "gutter": (PAST_THE_GUTTER, ["*", True]),
    # How far the page moved by itself since it began to load, scored from the
    # browser's own record of it as Cumulative Layout Shift.
    "layout shifts": (LAYOUT_SHIFTS, [SELECTORS["vendored"], CLS_LIMIT]),
    "audit": _AUDIT,
}


def geometry_probes(path, width):
    """The geometry's probes on ``path`` at ``width``, as ``(source, arg)``
    by check: the dashboard's readings only on the dashboard."""

    reads: dict[str, Callable[[], Any]] = {check: partial(_probe, probe) for check, probe in _GEOMETRY.items()}
    reads["axe"] = rules_read
    if path == "/":
        reads["readings"] = partial(_probe, (_READINGS_OF, SELECTORS["readings"]))
    return Probes(reads)


def survey_checks(path, width):
    """The checks the survey runs on ``path`` at ``width``, by name."""

    return [*geometry_probes(path, width), *palette_probes(), "fills"]


def _as_is(value):
    return value


# What turns a probe's value into its check's findings, where that is not the
# value itself.
AS_FINDINGS = {
    "tables": lambda report: report["found"],
    "audit": lambda report: [v for v in report["violations"] if v["rule"] in AUDIT_RULES],
    "readings": reading_problems,
}
