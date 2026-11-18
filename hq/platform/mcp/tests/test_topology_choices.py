"""get_topology names the lenses and trace directions a model may ask by, from
where they are declared; test_one_refusal_each_door refuses any other."""

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

    def test_no_lens_answers_the_counts_and_the_lenses(self):
        _, (answer,) = session(("get_topology", {}))

        self.assertFalse(answer.is_error)
        self.assertEqual(
            [lens["name"] for lens in answer.structured_content["lenses"]], [lens.name for lens in TOPOLOGY_LENSES]
        )
