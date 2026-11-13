"""A state drawn by `{% status %}` reads one way wherever it stands."""

import re

from django.template import engines
from django.template.autoreload import get_template_directories
from django.test import SimpleTestCase, tag

from hq.platform.application.infrastructure import RESOURCE_TONES
from hq.platform.application.tests.markup import elements
from hq.platform.application.ui import STATUS_VALUES
from hq.platform.core.templatetags.status_tags import severity_tone, status_pill


def _drawn(source: str, **context) -> str:
    return engines["django"].from_string(source).render(context)


class StatusPillTests(SimpleTestCase):
    def test_a_state_is_said_in_sentence_case_whatever_its_source_wrote(self):
        """Docker's "exited", a model's "Done" and a tag's "online" read alike."""

        for said, shown in (
            ("exited", "Exited"),
            ("online", "Online"),
            ("Done", "Done"),
            ("iOS", "iOS"),
            ("v1.2.3", "v1.2.3"),
            ("k3s ready", "k3s ready"),
        ):
            with self.subTest(said):
                pill = elements(_drawn("{% status state %}", state=said), "span")[0]
                self.assertEqual(pill.words, shown)

    def test_its_tone_names_the_class_it_is_colored_by(self):
        pill = elements(_drawn('{% status "Blocked" tone="attention" %}'), "span")[0]

        self.assertEqual(pill.attrs["class"].split(), ["pill", "pill-attention"])

    def test_a_resources_health_state_takes_the_tone_every_page_draws_it_in(self):
        """A record's "drifted" or "healthy" is a state, not a class the
        stylesheet colors, so it is said as its tone."""

        for state, tone in (("healthy", "good"), ("drifted", "serious"), ("pending", "attention")):
            with self.subTest(state):
                pill = elements(_drawn("{% status label tone=state %}", label="Working", state=state), "span")[0]
                self.assertIn(f"pill-{tone}", pill.attrs["class"].split())

    def test_several_parts_are_one_label_only_the_first_cased(self):
        pill = elements(_drawn('{% status record_type "not managed" %}', record_type="cname"), "span")[0]

        self.assertEqual(pill.words, "Cname not managed")

    def test_a_link_a_tip_and_data_attributes_are_drawn_on_the_pill(self):
        markup = _drawn('{% status "Problem" tone="attention" href="#here" tip="Why" data_origin="o-1" %}')

        pill = elements(markup, "a")[0]
        self.assertEqual(pill.attrs["href"], "#here")
        self.assertEqual(pill.attrs["data-tip"], "Why")
        self.assertEqual(pill.attrs["tabindex"], "0")
        self.assertEqual(pill.attrs["data-origin"], "o-1")

    def test_a_value_in_the_label_is_escaped(self):
        markup = status_pill("<b>exited</b>", tone="serious")

        self.assertNotIn("<b>", markup)

    def test_an_advisorys_severity_takes_one_tone_everywhere(self):
        self.assertEqual(
            [severity_tone(word) for word in ("critical", "HIGH", "moderate", "low", None)],
            ["serious", "serious", "attention", "neutral", "neutral"],
        )

    def test_a_state_drawn_without_its_pill_is_its_words_in_its_tone(self):
        """A figure that says which way it went keeps its words and its tone,
        with no pill around it; a tone with no color of its own is neutral."""

        for tone, shown in (("good", "good"), ("drifted", "serious"), ("archived", "neutral"), ("", "neutral")):
            with self.subTest(tone):
                drawn = elements(_drawn('{% status "+3 bpm" tone=tone pill=False %}', tone=tone), "span")[0]
                self.assertEqual(drawn.words, "+3 bpm")
                self.assertNotIn("pill", drawn.classes)
                self.assertIn(f"toned-{shown}", drawn.classes)

    def test_a_chip_is_a_name_said_as_written_in_no_tone(self):
        """A technology, a domain or a role a record carries is a name, so it
        keeps its own case ("django", "example.com") and takes no state's color."""

        for name in ("django", "example.com", "Reverse proxy"):
            with self.subTest(name):
                chip = elements(_drawn('{% status name chip=True title="Example" %}', name=name), "span")[0]
                self.assertEqual(chip.words, name)
                self.assertEqual(chip.attrs["title"], "Example")
                self.assertFalse({"pill", "toned"} & {word.split("-")[0] for word in chip.classes})

    def test_a_standing_state_is_its_words_in_its_tone_after_a_dot(self):
        """ "Allowed" or "Paused" reads as its words, no pill, led by a dot the
        stylesheet draws in the words' own tone."""

        for tone, shown in (("good", "good"), ("attention", "attention"), ("archived", "neutral")):
            with self.subTest(tone):
                drawn = elements(_drawn('{% status "allowed" tone=tone dot=True %}', tone=tone), "span")[0]
                self.assertEqual(drawn.words, "Allowed")
                self.assertNotIn("pill", drawn.classes)
                self.assertIn(f"toned-{shown}", drawn.classes)
                self.assertIn("toned-dot", drawn.classes)

    def test_a_dotted_state_in_context_ink_tones_only_its_dot(self):
        """A heading's or a menu's own words ("Tailnet", "Controller") keep the
        ink around them, and the dot alone says the state."""

        drawn = elements(_drawn('{% status "Tailnet" tone="good" dot=True ink="context" %}'), "span")[0]
        self.assertEqual(drawn.words, "Tailnet")
        self.assertIn("toned-good", drawn.classes)
        self.assertIn("toned-dot", drawn.classes)
        self.assertIn("toned-ink-context", drawn.classes)
        self.assertNotIn(
            "toned-ink-context", elements(_drawn('{% status "Up" tone="good" dot=True %}'), "span")[0].classes
        )

    def test_context_ink_is_only_for_a_dotted_state(self):
        for options in ({"ink": "context"}, {"ink": "context", "pill": False}, {"ink": "other", "dot": True}):
            with self.subTest(options), self.assertRaises(ValueError):
                status_pill("Up", tone="good", **options)

    def test_only_a_dotted_state_carries_the_dot(self):
        for source in ('{% status "Up" tone="good" %}', '{% status "Up" tone="good" pill=False %}'):
            with self.subTest(source):
                self.assertNotIn("toned-dot", elements(_drawn(source), "span")[0].classes)

    def test_a_state_led_by_its_mark_draws_the_mark_in_its_tone(self):
        """A cell's state ("1 serious", "Up to date") reads by its mark as well
        as its color: the mark inside the toned words, before them, and with
        context ink only the mark takes the tone."""

        html = _drawn('{% status "1 serious" tone="serious" pill=False icon="shield" %}')
        drawn = elements(html, "span")[0]
        self.assertEqual(drawn.words, "1 serious")
        self.assertIn("toned-serious", drawn.classes)
        self.assertNotIn("toned-ink-context", drawn.classes)
        (mark,) = elements(html, "svg")
        self.assertIn(drawn, mark.ancestors)
        self.assertLess(html.index("<svg"), html.index("1 serious"))

        quiet = elements(
            _drawn('{% status "Up to date" tone="good" pill=False icon="check-circle" ink="context" %}'), "span"
        )[0]
        self.assertIn("toned-good", quiet.classes)
        self.assertIn("toned-ink-context", quiet.classes)
        self.assertNotIn("toned-dot", quiet.classes)

    def test_a_mark_stands_only_before_a_states_words(self):
        for options in ({}, {"pill": False, "dot": True}, {"chip": True}):
            with self.subTest(options), self.assertRaises(ValueError):
                status_pill("Up", icon="check-circle", **options)

    def test_a_chip_refuses_a_tone(self):
        for options in ({"tone": "good"}, {"pill": False}, {"dot": True}):
            with self.subTest(options), self.assertRaises(ValueError):
                status_pill("django", chip=True, **options)


# A class a status draws, which a template writing it by hand would spell itself,
# and a state's tone spelled as a `control-` class: a state, a tone, or one filled in.
_TONE_WORDS = "|".join(sorted({*RESOURCE_TONES, *STATUS_VALUES, "unknown"}))
_STATUS_CLASS = re.compile(rf"^(pill|toned|worth)(-|$)|^control-(\{{\{{|({_TONE_WORDS})$)")
_CLASS = re.compile(r"""\bclass\s*=\s*(["'])(.*?)\1""", re.DOTALL)
# A class's words, a word chosen inside `{% if %}` among them.
_CLASS_WORDS = re.compile(r"\s+|\{%|%\}")


@tag("source", "tree")
class StatusDrawnByTheTagTests(SimpleTestCase):
    def test_no_template_draws_a_status_by_hand(self):
        """A pill, a toned figure or a state's words in its tone are
        `{% status %}`'s to draw, in the host and in every installed
        extension, so each says its state one way."""

        found = [
            f"{path.relative_to(root)}: {word}"
            for root in sorted(get_template_directories())
            for path in sorted(root.rglob("*.html"))
            for match in _CLASS.finditer(path.read_text(encoding="utf-8"))
            for word in _CLASS_WORDS.split(match.group(2))
            if _STATUS_CLASS.match(word)
        ]
        self.assertEqual(found, [], "Draw these with {% status ... %} (pill=False for a toned figure).")
