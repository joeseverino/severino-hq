"""How a thing stands is said in one vocabulary, held as each view is built."""

from datetime import date

from django.test import SimpleTestCase


def _shapes():
    """Each shape that says how a thing stands: how it is built in a tone, and
    the field it holds that tone in."""

    from ..card_items import Insight, Kpi, ListRow, TimelineItem
    from ..drawings import Share
    from ..finding_model import Finding
    from ..hq_context.band import Tile

    return (
        (lambda tone: Kpi("Net worth", "$1.00", tone=tone), "tone"),
        (lambda tone: Kpi("Net worth", "$1.00", "+$1.00", detail_tone=tone), "detail_tone"),
        (lambda tone: Insight(tone, "Finding", "Example is not answering", "", ""), "status"),
        (lambda tone: ListRow("Backup host", status=tone, badge="Up"), "status"),
        (lambda tone: TimelineItem(date(2026, 1, 1), "Renewal", status=tone), "status"),
        (lambda tone: Share("Owed", 1.0, 2, tone=tone), "tone"),
        (lambda tone: Finding("example-rule", "service:example.com", "Example", tone, "Why."), "severity"),
        (lambda tone: Tile("Runs on", "example", tone=tone), "tone"),
    )


class ToneTests(SimpleTestCase):
    def test_a_tone_written_as_its_word_is_held_as_its_member(self):
        from ..tone import Tone

        for build, field in _shapes():
            view = build("serious")
            with self.subTest(type(view).__name__, field=field):
                self.assertIs(getattr(view, field), Tone.SERIOUS)

    def test_a_word_that_is_no_tone_fails_where_it_is_written(self):
        for build, field in _shapes():
            with self.subTest(field, build=build), self.assertRaises(ValueError):
                build("goood")

    def test_the_worst_of_several_is_the_most_severe_and_none_is_good(self):
        from ..tone import Tone

        self.assertIs(Tone.worst(("good", "attention", "neutral")), Tone.ATTENTION)
        self.assertIs(Tone.worst(()), Tone.GOOD)
