"""A state drawn by `{% status %}` reads one way wherever it stands."""

import re

from django.template import engines
from django.template.autoreload import get_template_directories
from django.test import SimpleTestCase, tag

from hq.platform.application.markup_testing import elements
from hq.platform.application.tests.reads import reads
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

    def test_a_resources_health_state_takes_the_tone_every_page_draws_it_in(self):
        """A record's "drifted" or "healthy" is a state, not a class the
        stylesheet colors, so it is said as its tone."""

        from hq.platform.application.infrastructure import ResourceState

        for state in ResourceState:
            with self.subTest(state):
                pill = elements(_drawn("{% status label tone=state %}", label="Working", state=state), "span")[0]
                self.assertEqual(pill.attrs["class"].split(), ["pill", f"tone-{state.tone}"])

    def test_a_subjects_state_is_drawn_in_its_own_words_and_tone(self):
        """Every subject's states carry both (a machine's, a container's, a
        model's stored status), so a page names neither, and its pill is
        colored by its tone, never by its word."""

        from hq.platform.application.container_state import ContainerState
        from hq.platform.application.machines import MachineState
        from hq.platform.application.tone import Toned

        def subjects(kind):
            for each in kind.__subclasses__():
                yield each
                yield from subjects(each)

        states = {*MachineState, *ContainerState, *(state for kind in subjects(Toned) for state in kind)}
        for state in states:
            with self.subTest(f"{type(state).__qualname__}.{state.name}"):
                pill = elements(_drawn("{% status state %}", state=state), "span")[0]
                self.assertEqual(pill.attrs["class"].split(), ["pill", f"tone-{state.tone}"])
                self.assertEqual(pill.words.lower(), state.label.lower())

    def test_a_models_stored_states_each_take_one_of_the_four_tones(self):
        """A stored status (an asset's "retired", a message's "unread") is
        declared with its tone, and its pill says it in the model's label."""

        from hq.domains.assets.models import Asset
        from hq.domains.control_plane.provider_adapters.cloudflare import SubmissionStatus
        from hq.platform.core.models import AuditLog

        for state, label, tone in (
            (Asset.Status.RETIRED, "Retired", "neutral"),
            (SubmissionStatus.UNREAD, "Unread", "attention"),
            (AuditLog.Action.LOGIN_FAILED, "Login failed", "serious"),
        ):
            with self.subTest(state):
                pill = elements(_drawn("{% status state %}", state=state), "span")[0]
                self.assertEqual(pill.words, label)
                self.assertEqual(pill.attrs["class"].split(), ["pill", f"tone-{tone}"])

    def test_several_parts_are_one_label_only_the_first_cased(self):
        pill = elements(_drawn('{% status record_type "not managed" %}', record_type="cname"), "span")[0]

        self.assertEqual(pill.words, "Cname not managed")

    def test_an_advisorys_severity_takes_one_tone_everywhere(self):
        self.assertEqual(
            [severity_tone(word) for word in ("critical", "HIGH", "moderate", "low", None)],
            ["serious", "serious", "attention", "neutral", "neutral"],
        )

    def test_a_state_drawn_without_its_pill_is_its_words_in_its_tone(self):
        """A figure that says which way it went keeps its words and its tone,
        with no pill around it; no tone is neutral."""

        for tone, shown in (("good", "good"), ("attention", "attention"), ("", "neutral")):
            with self.subTest(tone):
                drawn = elements(_drawn('{% status "+3 bpm" tone=tone pill=False %}', tone=tone), "span")[0]
                self.assertEqual(drawn.words, "+3 bpm")
                self.assertNotIn("pill", drawn.classes)
                self.assertIn(f"tone-{shown}", drawn.classes)

    def test_a_chip_is_a_name_said_as_written_in_no_tone(self):
        """A technology, a domain or a role a record carries is a name, so it
        keeps its own case ("django", "example.com") and takes no state's color."""

        for name in ("django", "example.com", "Reverse proxy"):
            with self.subTest(name):
                chip = elements(_drawn('{% status name chip=True title="Example" %}', name=name), "span")[0]
                self.assertEqual(chip.words, name)
                self.assertEqual(chip.attrs["title"], "Example")
                self.assertFalse({"pill", "toned"} & {word.split("-")[0] for word in chip.classes})

    def test_context_ink_is_only_for_a_dotted_state(self):
        for options in ({"ink": "context"}, {"ink": "context", "pill": False}, {"ink": "other", "dot": True}):
            with self.subTest(options), self.assertRaises(ValueError):
                status_pill("Up", tone="good", **options)

    def test_a_mark_stands_only_before_a_states_words(self):
        for options in ({}, {"pill": False, "dot": True}, {"chip": True}):
            with self.subTest(options), self.assertRaises(ValueError):
                status_pill("Up", icon="check-circle", **options)

    def test_a_chip_refuses_a_tone(self):
        for options in ({"tone": "good"}, {"pill": False}, {"dot": True}):
            with self.subTest(options), self.assertRaises(ValueError):
                status_pill("django", chip=True, **options)

    def test_a_frame_in_a_tone_takes_the_class_its_status_is_colored_by(self):
        from hq.platform.application.tone import Tone

        for tone in (*Tone, ""):
            with self.subTest(tone):
                frame = _drawn("{{ tone|tone_class }}", tone=tone)
                (drawn,) = elements(_drawn('{% status "Example" tone=tone dot=True %}', tone=tone), "span")
                self.assertIn(frame, drawn.classes)

    def test_a_word_that_is_no_tone_is_refused_wherever_a_tone_colors_words(self):
        """A misspelled tone fails where it is drawn, never drawn as neutral,
        and a pill is never colored by a word of its own (a model's "archived",
        a resource's "drifted"): only a Tone or a subject's Toned state."""

        for source in (
            "{{ tone|tone_class }}",
            '{% status "Example" tone=tone %}',
            '{% status "Example" tone=tone dot=True %}',
            '{% status "Example" tone=tone pill=False %}',
            '{% status "Example" tone=tone icon="shield" pill=False %}',
        ):
            for word in ("goood", "archived", "drifted"):
                with self.subTest(source, word=word), self.assertRaises(ValueError):
                    _drawn(source, tone=word)


# A class a status draws, which a template writing it by hand would spell itself,
# and a state's tone spelled as a `control-` class: a state, a tone, or one filled in.
def _status_class() -> re.Pattern[str]:
    from hq.platform.application.infrastructure import ResourceState
    from hq.platform.application.tone import Tone

    words = "|".join(sorted({*ResourceState, *Tone}))
    return re.compile(r"^(pill|toned|worth)(-|$)|^control-(\{\{|(" + words + r")$)")


_CLASS = re.compile(r"""\bclass\s*=\s*(["'])(.*?)\1""", re.DOTALL)
# A class's words, a word chosen inside `{% if %}` among them.
_CLASS_WORDS = re.compile(r"\s+|\{%|%\}")


@tag("source", "tree")
@reads("**/templates/**")
class StatusDrawnByTheTagTests(SimpleTestCase):
    def test_no_template_draws_a_status_by_hand(self):
        """A pill, a toned figure or a state's words in its tone are
        `{% status %}`'s to draw, in the host and in every installed
        extension, so each says its state one way."""

        status_class = _status_class()
        found = [
            f"{path.relative_to(root)}: {word}"
            for root in sorted(get_template_directories())
            for path in sorted(root.rglob("*.html"))
            for match in _CLASS.finditer(path.read_text(encoding="utf-8"))
            for word in _CLASS_WORDS.split(match.group(2))
            if status_class.match(word)
        ]
        self.assertEqual(found, [], "Draw these with {% status ... %} (pill=False for a toned figure).")
