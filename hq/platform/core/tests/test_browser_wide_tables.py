"""The layout gate's table widths follow the page padding the stylesheet declares."""

from django.test import SimpleTestCase

from hq.platform.core.browser_tests import narrow_table, wide_tables


def css(pad="clamp(14px, 2.2vw, 28px)", card_space="--space-9", space=18, border=1):
    return (
        f":root {{ --content-pad: {pad}; {card_space}: {space}px; }}\n"
        f".two-col > section, .card {{ padding: var(--surface-inset, var(--space-8) var({card_space})); }}\n"
        f":is(.card) {{ border: var(--surface-border, {border}px solid var(--line)); }}\n"
    )


class WideTableWidthTests(SimpleTestCase):
    def test_the_width_is_where_a_carded_region_first_reaches_the_region(self):
        # A page's region is the viewport less 2.2vw and its scroll box's
        # border a side, 1000 wide from 1049;
        # a card's takes 19px more a side, from 1086: one width holds both.
        self.assertEqual(wide_tables(css(), 1000), (1000, (1086,)))

    def test_the_width_moves_with_the_region_and_the_padding(self):
        self.assertEqual(wide_tables(css(), 900), (900, (982,)))
        self.assertEqual(wide_tables(css(pad="clamp(14px, 2.2vw, 20px)"), 1000), (1000, (1078,)))

    def test_a_card_padded_far_from_the_page_is_held_at_both_widths(self):
        self.assertEqual(wide_tables(css(space=40), 1000), (1000, (1049, 1132)))

    def test_a_declaration_the_gate_cannot_read_fails_loudly(self):
        with self.assertRaisesMessage(AssertionError, "the content's padding"):
            wide_tables(css(pad="28px"), 1000)

    def test_the_narrow_width_is_where_a_pages_region_first_reaches_the_region(self):
        # 672 less 14.8px of padding and a 1px border a side is 640.4px.
        self.assertEqual(narrow_table(css(), 640), 672)
