"""audit:css-usage's reading of a sheet: where each rule is, which a used
range names, and why an unused one went unused."""

import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from hq.platform.core.tests.css_usage import (
    Sheet,
    blocks_of,
    code_points,
    report,
    sorted_unused,
    source_words,
    unused_bytes,
    used_blocks,
    written,
)

SHEET = """/* a comment with a { brace } in it */
@layer components {
  .card { color: red; }
  .ghost { content: "}{"; }
  .band {
    gap: 1px;
    & > .cell { margin: 0; }
  }
  @media print {
    .card { color: black; }
  }
  @media (width >= 40rem) {
    .wide-only { display: grid; }
  }
  [data-hidden-state] { display: none; }
}
"""
# The conditions that held on some page read: the width query did, print did not.
HELD = {"@media (width >= 40rem)"}


def block(blocks, prelude):
    return next(index for index, each in enumerate(blocks) if each.prelude == prelude)


def chromium_range(text, prelude):
    """The range Chromium's rule usage gives a rule, as UTF-16 offsets of its
    header's start and its body's end."""

    blocks = blocks_of(text)
    found = blocks[block(blocks, prelude)]
    return found.start, found.end - 1


class BlocksTests(SimpleTestCase):
    def test_every_rule_is_found_on_its_line_past_comments_strings_and_nesting(self) -> None:
        blocks = blocks_of(SHEET)
        lines = {each.prelude: each.line for each in blocks}
        self.assertEqual([each.line for each in blocks if each.prelude == ".card"], [3, 10])
        self.assertEqual(lines[".ghost"], 4)
        self.assertEqual(lines["& > .cell"], 7)
        self.assertEqual(lines["[data-hidden-state]"], 15)
        self.assertEqual(sum(each.style for each in blocks), 7)

    def test_a_rule_s_own_bytes_leave_out_the_rules_nested_in_it(self) -> None:
        blocks = blocks_of(SHEET)
        band, cell = blocks[block(blocks, ".band")], blocks[block(blocks, "& > .cell")]
        whole = len(SHEET[band.start : band.end].encode())
        self.assertEqual(band.own + cell.own, whole)
        self.assertEqual(blocks[cell.parent].prelude, ".band")

    def test_an_offset_past_a_character_outside_the_basic_plane_lands_on_its_index(self) -> None:
        text = '.a::before { content: "😀"; } .b { color: red; }'
        units = code_points(text)
        self.assertEqual(units(text.encode("utf-16-le").find(".b".encode("utf-16-le")) // 2), text.index(".b"))


class UsedTests(SimpleTestCase):
    def test_a_used_range_names_its_rule_and_a_parent_s_names_the_parent(self) -> None:
        blocks = blocks_of(SHEET)
        used, unmatched = used_blocks(blocks, [chromium_range(SHEET, ".band"), chromium_range(SHEET, ".ghost")])
        self.assertEqual(used, {block(blocks, ".band"), block(blocks, ".ghost")})
        self.assertEqual(unmatched, [])

    def test_declarations_after_a_nested_rule_name_the_rule_around_them(self) -> None:
        text = ".a { & .b { color: red; } margin: 0; }"
        blocks = blocks_of(text)
        at = text.index("margin")
        used, _ = used_blocks(blocks, [(at, at + len("margin: 0;"))])
        self.assertEqual(used, {block(blocks, ".a")})

    def test_a_range_on_a_media_query_s_condition_marks_no_rule(self) -> None:
        blocks = blocks_of(SHEET)
        at = SHEET.index("(width >= 40rem)")
        self.assertEqual(used_blocks(blocks, [(at, at + len("(width >= 40rem)"))]), (set(), []))

    def test_a_range_inside_no_rule_is_counted_unmatched(self) -> None:
        _, unmatched = used_blocks(blocks_of(SHEET), [(0, 5)])
        self.assertEqual(unmatched, [(0, 5)])


class SortedTests(SimpleTestCase):
    def sheet(self, used: list[str]) -> Sheet:
        blocks = blocks_of(SHEET)
        return Sheet("example", len(SHEET.encode()), blocks, {block(blocks, each) for each in used}, [])

    def test_each_unused_rule_is_dead_unreached_or_apart_by_what_it_asks_for(self) -> None:
        sheet = self.sheet([".card"])
        words = frozenset({"card", "band", "cell", "wide-only", "ghost"})
        kinds = {
            kind: {sheet.blocks[index].prelude for index in indices}
            for kind, indices in sorted_unused(sheet, words, HELD).items()
        }
        self.assertEqual(kinds["apart"], {".card"})
        self.assertEqual(kinds["dead"], {"[data-hidden-state]"})
        self.assertEqual(kinds["unreached"], {".ghost", ".band", "& > .cell", ".wide-only"})

    def test_a_nested_rule_in_a_dead_rule_is_dead(self) -> None:
        sheet = self.sheet([])
        kinds = sorted_unused(sheet, frozenset({"cell"}), HELD)
        self.assertIn(block(sheet.blocks, "& > .cell"), kinds["dead"])

    def test_a_class_built_from_a_prefix_or_a_dataset_key_is_written(self) -> None:
        sheet = self.sheet([])
        kinds = sorted_unused(sheet, frozenset({"wide-", "hiddenState"}), HELD)
        self.assertIn(block(sheet.blocks, ".wide-only"), kinds["unreached"])
        self.assertIn(block(sheet.blocks, "[data-hidden-state]"), kinds["unreached"])

    def test_an_at_rule_whose_rules_all_go_counts_with_them(self) -> None:
        sheet = self.sheet([])
        wide = block(sheet.blocks, ".wide-only")
        media = sheet.blocks[sheet.blocks[wide].parent]
        self.assertEqual(unused_bytes(sheet, [wide]), sheet.blocks[wide].own + media.own)

    def test_the_report_names_each_unused_rule_by_file_and_line(self) -> None:
        sheet = self.sheet([".card", ".band", "& > .cell", ".ghost", ".wide-only"])
        lines = report([sheet], frozenset({"card"}), HELD, 1, {})
        self.assertTrue(
            any("static/css/app/example.css:15" in line and "[data-hidden-state]" in line for line in lines)
        )
        self.assertFalse(any("example.css:3 " in line for line in lines))


class SourceWordsTests(SimpleTestCase):
    def test_a_prefix_counts_only_where_the_source_builds_a_name_from_it(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            templates = Path(root, "templates")
            templates.mkdir()
            (templates / "page.html").write_text('<p class="pill pill-{{ tone }} is-plain">', encoding="utf-8")
            words = source_words(Path(root))
        self.assertTrue(written("pill-warning", words))
        self.assertTrue(written("is-plain", words))
        self.assertFalse(written("is-featured", words))


class ConditionTests(SimpleTestCase):
    def test_a_keyframe_is_no_rule_to_use(self) -> None:
        blocks = blocks_of("@keyframes pulse { 0% { opacity: 1; } 100% { opacity: 0; } } .a { color: red; }")
        self.assertEqual([each.prelude for each in blocks if each.style], [".a"])

    def test_a_rule_of_a_selector_the_browser_did_not_parse_is_apart(self) -> None:
        text = ".bar::-moz-progress-bar { color: red; } .bar::-webkit-progress-value { color: red; }"
        blocks = blocks_of(text)
        sheet = Sheet("example", len(text), blocks, set(), [])
        held = {"selector(.bar::-webkit-progress-value)"}
        kinds = sorted_unused(sheet, frozenset({"bar"}), held)
        self.assertEqual([blocks[index].prelude for index in kinds["apart"]], [".bar::-moz-progress-bar"])
        self.assertEqual([blocks[index].prelude for index in kinds["unreached"]], [".bar::-webkit-progress-value"])
