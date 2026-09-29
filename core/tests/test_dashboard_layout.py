"""Highlight cards keep their figures in even rows at every card width."""

from __future__ import annotations

import re
from functools import cache
from math import ceil
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

_RULE = re.compile(r"(@container \(min-width: (\d+)px\) \{)|([^{}]+)\{([^{}]*)\}|(\})")
_HAS = re.compile(r":has\(> ([^)]*\)?[^)]*)\)")


@cache
def _column_rules() -> tuple[tuple[int, str, int], ...]:
    """(minimum container width, cell selector, columns), in source order."""

    css = (Path(settings.BASE_DIR) / "static" / "css" / "app.css").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    found, width = [], 0
    for match in _RULE.finditer(css):
        if match.group(1):
            width = int(match.group(2))
        elif match.group(5):
            width = 0
        elif ".highlight-card > .kpi-grid" in match.group(3):
            columns = re.search(r"--kpi-columns:\s*(\d+)", match.group(4))
            if not columns:
                continue
            for selector in match.group(3).split(","):
                cells = _HAS.search(selector)
                found.append((width, cells.group(1) if cells else "", int(columns.group(1))))
    return tuple(found)


def _matches(cells: str, count: int) -> bool:
    if not cells:
        return True
    if cells == ":only-child":
        return count == 1
    nth = int(re.search(r":nth-child\((\d+)\)", cells).group(1))
    return count == nth if ":last-child" in cells else count >= nth


def columns(count: int, width: int) -> int:
    chosen = 0
    for minimum, cells, value in _column_rules():
        if width >= minimum and _matches(cells, count):
            chosen = value
    return chosen


class HighlightGridTests(SimpleTestCase):
    def test_no_count_leaves_one_cell_alone_on_a_row(self):
        for count in (2, 4, 5, 6):
            for width in range(280, 1200, 10):
                used = columns(count, width)
                self.assertTrue(used, (count, width))
                self.assertNotEqual(count % used, 1, f"{count} cells at {width}px: {used} columns")

    def test_cells_are_never_narrower_than_the_card_allows(self):
        for count in range(2, 8):
            for width in range(330, 1200, 10):
                self.assertGreaterEqual(width / columns(count, width), 110 - 1, (count, width))

    def test_a_wide_card_holds_up_to_six_cells_in_one_row(self):
        for count in range(1, 7):
            self.assertEqual(ceil(count / columns(count, 1100)), 1, count)

    def test_four_cells_in_a_half_width_card_share_one_row(self):
        # Two cards a row at the content width leaves each card about 520px.
        self.assertEqual(columns(4, 520), 4)
        self.assertEqual(columns(6, 520), 3)

    def test_at_most_two_highlight_cards_share_a_row(self):
        css = (Path(settings.BASE_DIR) / "static" / "css" / "app.css").read_text(encoding="utf-8")
        content = int(re.search(r"\.content \{ max-width: (\d+)px", css).group(1))
        card = int(
            re.search(r"\.dashboard-highlights \{[^}]*minmax\(min\(100%, (\d+)px\)", css).group(1)
        )

        self.assertGreater(card * 3, content)
        self.assertIn(
            ".dashboard-highlights > .highlight-card:nth-child(odd):last-child "
            "{ grid-column: 1 / -1; }",
            css,
        )
