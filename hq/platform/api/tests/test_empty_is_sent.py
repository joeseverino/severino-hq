"""A value sent empty is a value, judged as the document declares it, on every
door: only one left out takes its default. A narrowing whose choices are
listed refuses one sent empty, and the retry key refuses one sent empty or in
any shape a key does not take."""

from django.test import TestCase

from hq.domains.projects.models import Project

from .doors import OPERATOR, Caller, at_the_door, each_door

READER = Caller("example-reader", "read")
WRITER = Caller("example-writer", "read write_projects")
PROJECT = {"name": "Once", "slug": "once", "status": "active"}


class EmptyIsSentTests(TestCase):
    def setUp(self):
        at_the_door(self)

    def test_a_narrowing_sent_empty_is_judged_by_its_choices(self):
        for caller in (READER, OPERATOR):
            for door in each_door(self, caller, "get_topology", "get_findings"):
                with door:
                    for tool, field in (("get_topology", "lens"), ("get_findings", "severity")):
                        answer = door(tool, **{field: ""})

                        self.assertEqual(answer["error"]["code"], "invalid_input", (tool, answer))

    def test_a_retry_key_sent_empty_or_out_of_shape_is_refused_as_a_key_and_acts_on_nothing(self):
        for caller in (WRITER, OPERATOR):
            for door in each_door(self, caller):
                for key in ("", "has a space", "x" * 129, 7):
                    with door:
                        answer = door.capability("project.create", PROJECT | {"idempotency_key": key})

                        self.assertEqual(answer["error"]["code"], "invalid_idempotency_key", (key, answer))
                        self.assertFalse(Project.objects.exists())
