"""Every check the layout gates hold, as probes with their arguments, and how a reading becomes findings."""

from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path
from typing import Any, override

from hq.platform.core.tests.browser.probes.axe import contrast_read, rules_read
from hq.platform.core.tests.browser.probes.boxes import (
    ASIDES_WRAPPED_WITH_ROOM,
    CRAMPED_HEADS,
    CUT_RINGS,
    FRAMES_OUT_OF_ROW,
    FRAMES_SHORT_OF_THEIR_COLUMN,
    HEAD_CONTROLS,
    INSET_IN_THE_FRAME,
    INTO_A_NEIGHBOR,
    OVERLAPS,
    PAGE_FRAMES,
    SHORT_OF_ITS_BUTTONS,
    SIDEWAYS,
    SMALL_TARGETS,
    TITLES_OFF_THEIR_START,
)
from hq.platform.core.tests.browser.probes.controls import (
    ACTIONS_OFF_THE_LINE,
    ACTIONS_STACKED_WITH_ROOM,
    CONTROL_HEIGHTS,
    WRAPPED_CONTROLS,
)
from hq.platform.core.tests.browser.probes.headings import HEADINGS_IN_CAPITALS, QUIET_LINKS_IN_THE_LINE
from hq.platform.core.tests.browser.probes.palette import BUTTON_EDGES, FIELD_CONTRAST, FIELD_EDGES
from hq.platform.core.tests.browser.probes.parts import (
    BARE_FOLDS,
    DIALOGS_WITHOUT_OPENERS,
    HIDDEN_IN_SUMMARIES,
    LINED_PAST_THE_NAME,
    LINKS_AT_REST,
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
    CUT_BESIDE_ROOM,
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
    CHIPS_PAST_THEIR_PAINT,
    CUT_DECISIONS,
    DETAIL_WORDS_IN_ROWS,
    FACTS_AGAINST_THEIR_FRAME,
    FACTS_SPREAD_APART,
    OVERLAPPING_TEXT,
    PAST_THE_CLAMP,
    RAISED_TEXT,
    TOUCHING_TEXT,
    VALUES_OFF_THEIR_ROW,
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

FACT_ROWS_READ = (
    VALUES_OFF_THEIR_ROW,
    [SELECTORS["fact_band"], SELECTORS["band_fact_cell"], SELECTORS["fact_value"], SELECTORS["unseen"]],
)

# A fact's detail: every part of a fact after its value.
_FACT_DETAIL = f"{SELECTORS['band_fact_cell']} > dd:not({SELECTORS['fact_value']})"

FACTS_APART_READ = (FACTS_SPREAD_APART, [SELECTORS["fact_band"], SELECTORS["band_fact_cell"], SELECTORS["unseen"]])

DETAIL_ROWS_READ = (DETAIL_WORDS_IN_ROWS, [_FACT_DETAIL, SELECTORS["unseen"]])

# A link in a fact's detail, or a word there that opens its tip, sits on the
# line of the words beside it, as a chip does.
DETAIL_LINKS_READ = (
    CHIPS_OFF_THEIR_LINE,
    [f"{_FACT_DETAIL} :is({SELECTORS['link']}, [tabindex])", SELECTORS["unseen"]],
)

# How far inside its frame a fact's words and mark stand at the least.
FACT_INSET = 8

FACTS_INSET_READ = (FACTS_AGAINST_THEIR_FRAME, [SELECTORS["band_fact_cell"], SELECTORS["unseen"], FACT_INSET])

HEADS_READ = (
    HEAD_CONTROLS,
    [SELECTORS["head_actions"], SELECTORS["head_menu"], SELECTORS["head_menu_panel"], SELECTORS["ask"]],
)


_READINGS_OF = f"(selector) => ({READINGS})([...document.querySelectorAll(selector)])"

# Every probe the layout invariants read on a page, by the check it answers.
_GEOMETRY = {
    "sideways": _SIDEWAYS_READ,
    "cells": CELLS,
    "actions on the first line": ACTIONS_OFF_THE_LINE,
    "row actions stacked with room": (
        ACTIONS_STACKED_WITH_ROOM,
        f"{SELECTORS['row_actions_cell']}, {SELECTORS['queue_actions_cell']}",
    ),
    "broken words": (BROKEN_WORDS, SELECTORS["identifier"]),
    "words broken in text": (BROKEN_WORDS_IN_TEXT, _IDENTIFIER),
    "broken identifiers": (BROKEN_TOKENS, [SELECTORS["cell_token"], SELECTORS["wraps_in_a_cell"]]),
    "overlaps": OVERLAPS,
    "into a neighbor": INTO_A_NEIGHBOR,
    "overlapping text": (OVERLAPPING_TEXT, SELECTORS["floating"]),
    "touching text": (TOUCHING_TEXT, [SELECTORS["floating"], SELECTORS["identifier"]]),
    "chips off their paint": (CHIPS_PAST_THEIR_PAINT, [SELECTORS["chip"], SELECTORS["unseen"]]),
    "tables": TABLES,
    "tables fit": (TABLES_THAT_DO_NOT_FIT, SELECTORS["table_scroll"]),
    "unseen columns": (UNSEEN_COLUMNS, SELECTORS["table_scroll"]),
    "control labels": _SPILLED_CONTROLS,
    "cell labels": _SPILLED_CELLS,
    "small targets": _SMALL_TARGETS_READ,
    "cut rings": (CUT_RINGS, SELECTORS["target"]),
    "past the clamp": PAST_THE_CLAMP,
    "cut decisions": (CUT_DECISIONS, [SELECTORS["decision_detail"], "*", SELECTORS["unseen"]]),
    "short of its buttons": (SHORT_OF_ITS_BUTTONS, [SELECTORS["decision_words"], SELECTORS["decision_buttons"]]),
    "cut code in text": (CUT_DECISIONS, [SELECTORS["running_text"], SELECTORS["code"], SELECTORS["unseen"]]),
    "cut with room": (CUT_WITH_ROOM, [SELECTORS["cut_box"], SELECTORS["name_box"], SELECTORS["unseen"]]),
    "cut beside room": (CUT_BESIDE_ROOM, SELECTORS["unseen"]),
    "cut and closed": (CUT_AND_CLOSED, SELECTORS["unseen"]),
    "cut when open": (CUT_WHEN_OPEN, SELECTORS["unseen"]),
    "changes": CHANGES_READ,
    "chips": CHIPS_READ,
    "fact rows": FACT_ROWS_READ,
    "facts together": FACTS_APART_READ,
    "fact details in words": DETAIL_ROWS_READ,
    "links in a fact's detail": DETAIL_LINKS_READ,
    "facts inside their frame": FACTS_INSET_READ,
    "raised text": (RAISED_TEXT, SELECTORS["unseen"]),
    "heads": HEADS_READ,
    "section heads": (CRAMPED_HEADS, SELECTORS["section_head"]),
    "section asides": (ASIDES_WRAPPED_WITH_ROOM, SELECTORS["section_head"]),
    "inset in the frame": (INSET_IN_THE_FRAME, SELECTORS["section_body"]),
    "frames in a row": (FRAMES_OUT_OF_ROW, [SELECTORS["section_head"], SELECTORS["section_body"]]),
    "frames span their column": (FRAMES_SHORT_OF_THEIR_COLUMN, [PAGE_FRAMES, SELECTORS["floating"]]),
    "titles at the head's start": (TITLES_OFF_THEIR_START, SELECTORS["section_head"]),
    "control words on one line": (
        WRAPPED_CONTROLS,
        [SELECTORS["pressed"], SELECTORS["choice"], SELECTORS["unseen"], SELECTORS["vendored"]],
    ),
    "one height to a row of controls": (
        CONTROL_HEIGHTS,
        [SELECTORS["pressed"], SELECTORS["fillable"], SELECTORS["vendored"]],
    ),
    "dialogs": (DIALOGS_WITHOUT_OPENERS, [SELECTORS["dialog"], SELECTORS["dialog_opener"]]),
    "select arrows": (SELECT_ARROWS, [SELECTORS["dropdown"], SELECTORS["vendored"]]),
    "link underlines": (PARTS_NOT_UNDERLINED, [SELECTORS["link"], SELECTORS["vendored"]]),
    "links at rest": (LINKS_AT_REST, [SELECTORS["link"], SELECTORS["vendored"]]),
    "lined past the name": (
        LINED_PAST_THE_NAME,
        [SELECTORS["link"], SELECTORS["vendored"], SELECTORS["tag"], SELECTORS["mark"]],
    ),
    "lists of facts": (WRAPPED_BESIDE_A_PAIR, SELECTORS["fact_list"]),
    "bare folds": (BARE_FOLDS, [SELECTORS["section_fold"], SELECTORS["drawn_when_opened"]]),
    "headings in capitals": (HEADINGS_IN_CAPITALS, SELECTORS["kicker"]),
    "quiet links in the line": (QUIET_LINKS_IN_THE_LINE, SELECTORS["quiet_link"]),
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


# The checks of what a table draws and holds, which the sweep of every width
# (`BrowserGate.table_defects_at_every_width`) reads at each shape of the
# layout with the rows a reader can open opened.
TABLE_CHECKS = (
    "cells",
    "broken words",
    "broken identifiers",
    "tables",
    "tables fit",
    "unseen columns",
    "cell labels",
    "cut with room",
    "cut beside room",
    "cut and closed",
    "cut when open",
)


def table_probes():
    """The table checks' probes, as ``(source, arg)`` by check."""

    return {check: _probe(_GEOMETRY[check]) for check in TABLE_CHECKS}


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
