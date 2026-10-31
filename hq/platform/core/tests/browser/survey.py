"""Every check the layout gates hold, as probes with their arguments, and how a reading becomes findings."""

from pathlib import Path

from hq.platform.application.asks import READ_WAITING
from hq.platform.core.tests.browser.probes.boxes import (
    HEAD_CONTROLS,
    INTO_A_NEIGHBOR,
    NOTES_OUT_OF_FLOW,
    OVERLAPS,
    SIDEWAYS,
    SMALL_TARGETS,
)
from hq.platform.core.tests.browser.probes.palette import (
    BUTTON_EDGES,
    CONTRAST,
    FIELD_CONTRAST,
    FIELD_EDGES,
    LARGE_TEXT_CONTRAST,
    TEXT_CONTRAST,
)
from hq.platform.core.tests.browser.probes.parts import (
    ASKS_THAT_GROW_THEIR_ROW,
    DIALOGS_WITHOUT_OPENERS,
    HIDDEN_IN_SUMMARIES,
    READINGS,
    SPILLED_LABELS,
    STRANDED_EVIDENCE,
    WRAPPED,
    WRAPPED_BESIDE_A_PAIR,
    reading_problems,
)
from hq.platform.core.tests.browser.probes.shifts import CLS_LIMIT, LAYOUT_SHIFTS
from hq.platform.core.tests.browser.probes.tables import (
    CELLS,
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
)
from hq.platform.core.tests.browser.reading import _probe
from hq.platform.core.tests.browser.selectors import SELECTORS
from hq.platform.core.tests.browser.widths import TARGET_MIN

AUDIT = Path(__file__).parents[2] / "browser_audit.js"

# The rules from browser_audit.js that find what a reader loses: a scrollbar
# no one asked for, and content clipped out of sight. Its other rules judge
# density and evenness, which dev:look shows by eye rather than the gate.
AUDIT_RULES = {"unwanted-scrollbar", "clipped-content"}

_AUDIT = (
    "async () => { const audit = "
    + AUDIT.read_text(encoding="utf-8").strip()
    + ";\nreturn await audit({ evaluate: (probe) => probe() }); }"
)

# What is an identifier rather than a word, which the broken-word rule leaves
# to break where it must.
_IDENTIFIER = "code, kbd, samp, time, .token, [data-entity]"

_CONTRAST_READ = (CONTRAST, [SELECTORS["contrast_scope"], TEXT_CONTRAST, LARGE_TEXT_CONTRAST])

_FIELDS_READ = (FIELD_EDGES, [SELECTORS["fillable"], FIELD_CONTRAST, SELECTORS["pressed"]])

_BUTTON_EDGES_READ = (BUTTON_EDGES, SELECTORS["pressed"])

# Every check whose verdict depends on the palette the page is drawn in.
_PALETTE = {
    "contrast": _CONTRAST_READ,
    "fields": _FIELDS_READ,
    "button edges": _BUTTON_EDGES_READ,
}

_SIDEWAYS_READ = (SIDEWAYS, SELECTORS["sideways"])

_SPILLED_CONTROLS = (SPILLED_LABELS, SELECTORS["control"])

_SPILLED_CELLS = (SPILLED_LABELS, SELECTORS["cell_content"])


EVIDENCE_READ = (STRANDED_EVIDENCE, [SELECTORS["check_statement"], SELECTORS["check_evidence"]])

SUMMARIES_READ = (HIDDEN_IN_SUMMARIES, SELECTORS["summary_value"])


_SMALL_TARGETS_READ = (SMALL_TARGETS, [SELECTORS["target"], TARGET_MIN])

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
    "notes in flow": (NOTES_OUT_OF_FLOW, [SELECTORS["note"], SELECTORS["floating"]]),
    "overlapping text": (OVERLAPPING_TEXT, SELECTORS["floating"]),
    "tables": TABLES,
    "tables fit": (TABLES_THAT_DO_NOT_FIT, SELECTORS["table_scroll"]),
    "unseen columns": (UNSEEN_COLUMNS, SELECTORS["table_scroll"]),
    "control labels": _SPILLED_CONTROLS,
    "cell labels": _SPILLED_CELLS,
    "small targets": _SMALL_TARGETS_READ,
    "past the clamp": PAST_THE_CLAMP,
    "cut decisions": (CUT_DECISIONS, SELECTORS["decision_whole"]),
    "changes": CHANGES_READ,
    "chips": CHIPS_READ,
    "heads": HEADS_READ,
    "dialogs": (DIALOGS_WITHOUT_OPENERS, [SELECTORS["dialog"], SELECTORS["dialog_opener"]]),
    "asks in rows": (ASKS_THAT_GROW_THEIR_ROW, [SELECTORS["row_ask"], READ_WAITING]),
    "lists of facts": (WRAPPED_BESIDE_A_PAIR, SELECTORS["fact_list"]),
    "one line": (WRAPPED, SELECTORS["one_line"]),
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

    probes = {check: _probe(probe) for check, probe in _GEOMETRY.items()}
    if path == "/":
        probes["readings"] = (_READINGS_OF, SELECTORS["readings"])
    return probes


def survey_checks(path, width):
    """The checks the survey runs on ``path`` at ``width``, by name."""

    return [*geometry_probes(path, width), *_PALETTE, "fills"]


def _as_is(value):
    return value


# What turns a probe's value into its check's findings, where that is not the
# value itself.
AS_FINDINGS = {
    "tables": lambda report: report["found"],
    "audit": lambda report: [v for v in report["violations"] if v["rule"] in AUDIT_RULES],
    "contrast": lambda report: report["found"] if report["sampled"] else ["no text sampled"],
    "readings": reading_problems,
}
