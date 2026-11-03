"""get_topology names the lenses and trace directions a model may ask by, from
where they are declared, and refuses any other before it derives the graph."""

from unittest import mock

from django.test import TestCase

from hq.platform.application.topology import TOPOLOGY_LENSES, TRACE_DIRECTIONS

from .test_findings_choices import session


def _enum(properties, name):
    """The values ``name`` takes, whether or not it may also be null."""

    return next(option["enum"] for option in (properties[name], *properties[name].get("anyOf", ())) if "enum" in option)


class TopologyChoicesTests(TestCase):
    def test_the_lens_and_direction_choices_are_the_declared_ones(self):
        listed, _ = session()
        properties = listed["get_topology"].input_schema["properties"]

        self.assertEqual(_enum(properties, "lens"), [lens.name for lens in TOPOLOGY_LENSES])
        self.assertEqual(_enum(properties, "direction"), list(TRACE_DIRECTIONS))

    def test_a_lens_or_direction_none_declares_is_refused_before_the_graph_is_derived(self):
        with mock.patch("hq.platform.application.topology.derive_topology") as derived:
            _, answers = session(
                ("get_topology", {"lens": "example-unknown"}),
                ("get_topology", {"focus": "resource:example", "direction": "sideways"}),
            )

        for answer in answers:
            with self.subTest(answer=answer):
                self.assertTrue(answer.is_error)
                self.assertEqual(answer.structured_content["error"]["code"], "invalid_input")
        derived.assert_not_called()

    def test_no_lens_answers_the_counts_and_the_lenses(self):
        _, (answer,) = session(("get_topology", {}))

        self.assertFalse(answer.is_error)
        self.assertEqual(
            [lens["name"] for lens in answer.structured_content["lenses"]], [lens.name for lens in TOPOLOGY_LENSES]
        )
