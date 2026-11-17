"""A delete runs only when its caller names what it deletes twice, and every delete refuses one alike."""

from django.test import TestCase

from hq.domains.projects.models import Project

from ..capabilities import execute_capability
from ..security import OPERATOR_CAPABILITIES, Principal

TARGETS = {"project.delete": "example-site", "contact.submission.delete": 2}


class ConfirmTests(TestCase):
    def test_a_delete_without_its_confirm_is_refused_the_same_way_by_every_delete(self):
        Project.objects.create(name="Example site", slug="example-site")
        principal = Principal("operator", "cli", OPERATOR_CAPABILITIES)

        for payload in ({"confirm": "something-else"}, {}):
            with self.subTest(payload=payload):
                refused = {
                    name: execute_capability(name, payload, principal=principal, target=target)["error"]
                    for name, target in TARGETS.items()
                }

                # The message names the capability; what was refused, and why, is the same.
                said = {name: (error["code"], error.get("details")) for name, error in refused.items()}
                self.assertEqual(said["project.delete"][0], "invalid_input")
                self.assertEqual(said["project.delete"], said["contact.submission.delete"])
        self.assertTrue(Project.objects.filter(slug="example-site").exists())
