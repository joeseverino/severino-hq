"""A machine read that names something HQ does not have is refused with the
choices that would narrow it, and a read with no narrowing answers what the
estate holds rather than all of it."""

from django.test import TestCase
from pydantic import ValidationError

from hq.platform.core.testing import READER, declare

from ..projection import QueryRefusal
from ..record_errors import RecordNotFound
from ..topology import derive_topology, topology
from .host_only import host_only


class TopologyNarrowingTests(TestCase):
    def setUp(self):
        declare("lonely-record", "adguard.rewrite", {"domain": "lonely.example.test", "answer": "192.0.2.10"})

    def test_a_focus_outside_the_lens_is_refused(self):
        with host_only():
            every = {node.id for node in derive_topology(principal=READER).nodes}
            kept = {node["id"] for node in topology(principal=READER, lens="unobserved-resources")["nodes"]}
        outside = sorted(every - kept)
        self.assertTrue(outside)

        with host_only(), self.assertRaises((QueryRefusal, ValidationError, RecordNotFound)):
            topology(principal=READER, lens="unobserved-resources", focus=outside[0])
