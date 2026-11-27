"""The Agents page opens on the rules that differ from the default, so a save
posts only the rules drawn, and a rule not posted is left as it holds."""

from django.test import TestCase
from django.urls import reverse

from hq.domains.control_plane.models import CapabilityRule
from hq.platform.application.capability_policy import Rule, field_name
from hq.platform.application.markup_testing import all_elements
from hq.platform.core.models import AgentIdentity
from hq.platform.core.testing import operator


class FoldedSaveTests(TestCase):
    def test_a_save_with_the_fold_never_opened_changes_no_rule(self):
        owner = operator(self.client, "owner", email="owner@example.test")
        AgentIdentity.objects.create(client_id="example-agent", granted=["read", "write_projects", "delete_projects"])
        CapabilityRule.objects.create(
            subject="example-agent", capability="project.delete", rule=Rule.DENY, changed_by=owner
        )
        before = set(CapabilityRule.objects.values_list("subject", "capability", "rule"))

        # What the form posts as drawn: each field's value, a select's the option it opens on.
        posted = {}
        for element in all_elements(self.client.get(reverse("agent_policy"))):
            name = element.attrs.get("name", "")
            if element.tag == "input" and name and any("data-policy" in held.attrs for held in element.ancestors):
                posted[name] = element.attrs.get("value", "")
            elif element.tag == "option" and "selected" in element.attrs:
                posted[element.ancestors[-1].attrs["name"]] = element.attrs["value"]
        self.client.post(reverse("agent_policy"), posted)

        self.assertIn(field_name("example-agent", "project.delete"), posted)
        self.assertEqual(set(CapabilityRule.objects.values_list("subject", "capability", "rule")), before)
