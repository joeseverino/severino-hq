"""A group of a table's rows is headed by ``{% row_group %}`` and nothing else,
so every group row spans its table, keeps the rows' edge and can fold alike."""

import re

from django.test import SimpleTestCase, tag

from hq.platform.application.tests.source import template_files

PRIMITIVE = "partials/_row_group.html"

# A group row is known by its class or by its heading's scope.
_HAND_DRAWN = re.compile(r'\bclass="[^"]*\brow-group\b[^"]*"|\bscope="rowgroup"')


def hand_drawn_groups(source: str) -> int:
    return len(_HAND_DRAWN.findall(source))


@tag("source", "tree")
class RowGroupTests(SimpleTestCase):
    def test_every_group_row_is_drawn_by_the_tag(self):
        found = {
            name: count
            for name, path in template_files()
            if name != PRIMITIVE and (count := hand_drawn_groups(path.read_text(encoding="utf-8")))
        }
        self.assertEqual(found, {}, "Head these groups with {% row_group label count %}.")

    def test_a_hand_drawn_group_row_is_refused(self):
        self.assertEqual(hand_drawn_groups('<tr class="queue row-group"><th>Group</th></tr>'), 1)
        self.assertEqual(hand_drawn_groups('<tr><th colspan="3" scope="rowgroup">Group</th></tr>'), 1)
        self.assertEqual(hand_drawn_groups('{% row_group "Group" 2 %}<tr class="row-groupish"></tr>'), 0)
