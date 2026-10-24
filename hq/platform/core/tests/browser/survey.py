"""Every check the layout gates hold, as probes with their arguments, and how a reading becomes findings."""

from pathlib import Path

from hq.platform.core.tests.browser.probes.boxes import (
    EMPTY_FRAMES,
    HOLED_FORMS,
    NESTED_FRAMES,
    OVERLAPS,
    OVERRULED_ALIGNMENT,
    PAIRS,
    SIDEWAYS,
    SMALL_TARGETS,
    UNDER_ITS_LABEL,
    UNEVEN_BANDS,
)
from hq.platform.core.tests.browser.probes.cut_names import (
    CUT_NAMES,
    CUT_WITH_ROOM,
    PADDED_CUTS,
    PAST_THE_CLAMP,
    SPLIT_NAMES,
)
from hq.platform.core.tests.browser.probes.palette import (
    BUTTON_EDGES,
    CONTRAST,
    FAINT_LINES,
    FIELD_EDGES,
    FIELD_CONTRAST,
    LARGE_TEXT_CONTRAST,
    LINE_CONTRAST,
    TEXT_CONTRAST,
)
from hq.platform.core.tests.browser.probes.parts import (
    HIDDEN_IN_SUMMARIES,
    LONE_ARROWS,
    NESTED_DISCLOSURES,
    RAISED_LINKS,
    READINGS,
    ROUTE_LINE_ENDS,
    SPILLED_LABELS,
    STRANDED_EVIDENCE,
    SUMMARY_MARKS,
    reading_problems,
)
from hq.platform.core.tests.browser.probes.shifts import LAYOUT_SHIFTS
from hq.platform.core.tests.browser.probes.tables import (
    CELLS,
    CELLS_OFF_THE_FIRST_LINE,
    EMPTY_COLUMNS,
    PAST_THE_GUTTER,
    ROW_HEIGHTS,
    TABLES,
    TABLES_THAT_DO_NOT_FIT,
    UNPADDED_CELLS,
    UNSEEN_COLUMNS,
    ROW_LINES,
)
from hq.platform.core.tests.browser.probes.text import (
    ASIDE_BESIDE_ITS_HEADING,
    BROKEN_WORDS,
    BROKEN_WORDS_IN_TEXT,
    LONE_ASIDE_WORDS,
    OVERLAPPING_TEXT,
    SQUEEZED_TITLES,
    WRAPPED_PHRASES,
)
from hq.platform.core.tests.browser.probes.tokens import NAMES_IN_TRACKS, STRANDED_MARKS, TOKEN_BREAKS
from hq.platform.core.tests.browser.reading import _probe
from hq.platform.core.tests.browser.selectors import TOKEN_SELECTORS, SELECTORS
from hq.platform.core.tests.browser.widths import TARGET_MIN, WIDE, WIDE_REGION

AUDIT = Path(__file__).parents[2] / "browser_audit.js"

# Structural rules from browser_audit.js that hold on every page at
# every width. A card with a band of empty frame under its last line
# (empty-card-bottom) is one: a card is as tall as what it holds. The audit's
# other density heuristics (card-too-wide, row-height-hole, horizontal-slack)
# judge proportions that depend on how much data a page has, so they stay a
# by-eye tool rather than a gate.
AUDIT_RULES = {
    "unwanted-scrollbar",
    "clipped-content",
    "empty-card-bottom",
    "uneven-controls",
    "dead-tooltip",
    "seam-mismatch",
    "sentence-in-a-chip-row",
}

_AUDIT = (
    "async () => { const audit = "
    + AUDIT.read_text(encoding="utf-8").strip()
    + ";\nreturn await audit({ evaluate: (probe) => probe() }); }"
)

# What is an identifier, held to its own rule (`a_token_breaks_only_where_it_may`)
# rather than a word's.
_IDENTIFIER = "code, kbd, samp, time, .token, [data-entity]"

# Probes read by more than one rule or check, with their arguments.
_CUT_NAMES_READ = (
    CUT_NAMES,
    [
        SELECTORS["table_scroll"],
        SELECTORS["cell_name"],
        SELECTORS["sole_token"],
        SELECTORS["context_line"],
        WIDE_REGION,
    ],
)

_CUT_WITH_ROOM_READ = (
    CUT_WITH_ROOM,
    [SELECTORS["table_scroll"], SELECTORS["open_row"], SELECTORS["context_line"], SELECTORS["phrase_part"]],
)

_SPLIT_NAMES_READ = (
    SPLIT_NAMES,
    [
        SELECTORS["table_scroll"],
        SELECTORS["cell_name"],
        SELECTORS["context_line"],
        WIDE_REGION,
        SELECTORS["token_part"],
    ],
)

_FRAMES_READ = (NESTED_FRAMES, [SELECTORS["frame_exempt"], SELECTORS["frame_exempt_within"]])

_CONTRAST_READ = (CONTRAST, [SELECTORS["contrast_scope"], TEXT_CONTRAST, LARGE_TEXT_CONTRAST])

_LINES_READ = (FAINT_LINES, [SELECTORS["contrast_scope"], LINE_CONTRAST])

_FIELDS_READ = (FIELD_EDGES, [SELECTORS["fillable"], FIELD_CONTRAST, SELECTORS["pressed"]])

_BUTTON_EDGES_READ = (BUTTON_EDGES, SELECTORS["pressed"])

# Every check whose verdict depends on the palette the page is drawn in.
_PALETTE = {
    "frames": _FRAMES_READ,
    "contrast": _CONTRAST_READ,
    "lines": _LINES_READ,
    "fields": _FIELDS_READ,
    "button edges": _BUTTON_EDGES_READ,
}

_SIDEWAYS_READ = (SIDEWAYS, SELECTORS["sideways"])

_SPILLED_CONTROLS = (SPILLED_LABELS, SELECTORS["control"])

_SPILLED_CELLS = (SPILLED_LABELS, SELECTORS["cell_content"])

_PHRASES_READ = (WRAPPED_PHRASES, SELECTORS["one_line"])

ROUTE_ENDS_READ = (ROUTE_LINE_ENDS, [SELECTORS["route_line"], SELECTORS["route_mark"]])

EVIDENCE_READ = (STRANDED_EVIDENCE, [SELECTORS["check_statement"], SELECTORS["check_evidence"]])

SUMMARIES_READ = (HIDDEN_IN_SUMMARIES, SELECTORS["summary_value"])

_UNDER_LABELS_READ = (UNDER_ITS_LABEL, SELECTORS["labeled"])

_SMALL_TARGETS_READ = (SMALL_TARGETS, [SELECTORS["target"], TARGET_MIN])

_FORM_ROWS_READ = (HOLED_FORMS, [SELECTORS["form"], SELECTORS["field"]])

_EMPTY_FRAMES_READ = (EMPTY_FRAMES, [SELECTORS["frame"], SELECTORS["frame_content"]])

_READINGS_OF = f"(selector) => ({READINGS})([...document.querySelectorAll(selector)])"

# Every probe the layout invariants read on a page, by the check it answers.
_GEOMETRY = {
    "sideways": _SIDEWAYS_READ,
    "cells": CELLS,
    "broken words": BROKEN_WORDS,
    "words broken in text": (BROKEN_WORDS_IN_TEXT, _IDENTIFIER),
    "wrapped phrases": _PHRASES_READ,
    "overlaps": OVERLAPS,
    "overlapping text": (OVERLAPPING_TEXT, SELECTORS["floating"]),
    "grid rows": (PAIRS, SELECTORS["paired"]),
    "chip alignment": (OVERRULED_ALIGNMENT, SELECTORS["chip"]),
    "form rows": _FORM_ROWS_READ,
    "bands": (UNEVEN_BANDS, [SELECTORS["band"], SELECTORS["band_aside"]]),
    "band padding": (UNPADDED_CELLS, SELECTORS["band_cell"]),
    "tables": TABLES,
    "tables fit": (TABLES_THAT_DO_NOT_FIT, SELECTORS["table_scroll"]),
    "unseen columns": (UNSEEN_COLUMNS, SELECTORS["table_scroll"]),
    "empty columns": EMPTY_COLUMNS,
    "row heights": (ROW_HEIGHTS, ROW_LINES),
    "row alignment": (CELLS_OFF_THE_FIRST_LINE, SELECTORS["table_scroll"]),
    "names cut whole": _CUT_NAMES_READ,
    "short names split": _SPLIT_NAMES_READ,
    "cut with room": _CUT_WITH_ROOM_READ,
    "padded cuts": (PADDED_CUTS, SELECTORS["cut_box"]),
    "control labels": _SPILLED_CONTROLS,
    "cell labels": _SPILLED_CELLS,
    "raised links": RAISED_LINKS,
    "lone arrows": (LONE_ARROWS, SELECTORS["external_link"]),
    "small targets": _SMALL_TARGETS_READ,
    "row disclosures": NESTED_DISCLOSURES,
    "summary marks": SUMMARY_MARKS,
    "tokens": (TOKEN_BREAKS, TOKEN_SELECTORS),
    "stranded marks": (STRANDED_MARKS, SELECTORS["seam"]),
    "names in tracks": (NAMES_IN_TRACKS, ".token, .whole, .cut"),
    "values under labels": _UNDER_LABELS_READ,
    "heading asides": (ASIDE_BESIDE_ITS_HEADING, SELECTORS["heading_aside"]),
    "lone aside words": (LONE_ASIDE_WORDS, SELECTORS["any_aside"]),
    "past the clamp": PAST_THE_CLAMP,
    "titles": (SQUEEZED_TITLES, [SELECTORS["page_title"], SELECTORS["page_actions"], SELECTORS["page_head"]]),
    # Every box and every line of text, not only a card's or code's.
    "gutter": (PAST_THE_GUTTER, ["*", True]),
    # Whatever moved by itself since the page began to load, read from the
    # browser's record of it, so nothing a script does after the first frame
    # goes unseen.
    "layout shifts": (LAYOUT_SHIFTS, SELECTORS["vendored"]),
    "audit": _AUDIT,
}


def geometry_probes(path, width):
    """The geometry's probes on ``path`` at ``width``, as ``(source, arg)``
    by check: a wide frame is judged only where it has the room, and the
    dashboard's readings only on the dashboard."""

    probes = {check: _probe(probe) for check, probe in _GEOMETRY.items()}
    if width >= WIDE:
        probes["wide frames"] = _EMPTY_FRAMES_READ
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
