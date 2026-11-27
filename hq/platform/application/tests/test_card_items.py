"""A figure says which way it went and where its line leads, through the one band that draws it."""

from django.test import SimpleTestCase


class KpiLinkTests(SimpleTestCase):
    def test_a_line_with_a_link_needs_its_words(self):
        from ..card_items import Kpi

        with self.assertRaises(ValueError):
            Kpi("Expenses", "$120.00", detail_url="/example/expenses/")


class AnswerTests(SimpleTestCase):
    """A domain object's value reaches a fact as its short answer over its reasons."""

    def test_a_sentence_as_the_answer_and_a_tone_without_its_reason_are_refused(self):
        from ..card_items import Answer

        for made in (lambda: Answer("Can't check: no package list"), lambda: Answer("2 mounts", tone="attention")):
            with self.subTest(), self.assertRaises(ValueError):
                made()
