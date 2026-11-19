"""get_topology answers the lenses a model may ask by, as they are declared."""

from django.test import TestCase

from hq.platform.application.topology import TOPOLOGY_LENSES

from .test_findings_choices import session


class TopologyChoicesTests(TestCase):
    def test_no_lens_answers_the_counts_and_the_lenses(self):
        _, (answer,) = session(("get_topology", {}))

        self.assertFalse(answer.is_error)
        self.assertEqual(
            [lens["name"] for lens in answer.structured_content["lenses"]], [lens.name for lens in TOPOLOGY_LENSES]
        )
