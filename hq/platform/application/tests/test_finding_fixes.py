"""Every finding carries its fix: an operation through the gated queue, or the
exact thing a person does."""

import json

from django.test import TestCase, override_settings

from hq.domains.control_plane.models import ManagedResource
from hq.domains.control_plane.provider_adapters.portainer import CONTAINER_KIND

from ..capabilities import execute_capability
from ..findings import RULES
from ..finding_model import FindingRule
from ..infrastructure import NotFoundError, PolicyError
from ..inventory_testing import store
from ..policy_fixes import policy_without_empty_groups, request_empty_groups_removal
from ..security import AuthorizationError, Capability, Principal, cli_principal, mcp_principal
from .test_approvals import POLICY_KEY, an_operator, declare_policy
from .test_tailnet_posture import policy, raised, tailnet_connection

READER = Principal("reader", "test", frozenset({Capability.READ}))


def steps_text(finding) -> str:
    """Every word of a serialized finding's operator steps."""

    return " ".join(
        " ".join((step["label"], step["command"], *step["notes"]))
        for step in finding["operator_steps"]
    )


def document(**parts) -> str:
    return json.dumps(
        {
            "groups": {"group:empty": [], "group:admins": ["someone@example.com"]},
            "grants": [
                {"src": ["group:empty", "group:admins"], "dst": ["tag:server"], "ip": ["tcp:22"]},
                {"src": ["group:empty"], "dst": ["tag:server"], "ip": ["tcp:443"]},
            ],
            "ssh": [
                {"action": "check", "src": ["group:empty"], "dst": ["tag:server"],
                 "users": ["root"]},
            ],
            **parts,
        }
    )


class EveryRuleSaysHowTests(TestCase):
    def test_a_rule_without_an_operator_action_is_refused(self):
        with self.assertRaises(ValueError):
            FindingRule("example", "Example", "attention", lambda estate: (), "  ")

    def test_every_rule_declares_one(self):
        for rule in RULES:
            self.assertTrue(rule.operator_action.strip(), rule.name)
            self.assertNotIn("—", rule.operator_action, rule.name)

    def test_a_finding_without_its_own_action_carries_the_rules(self):
        tailnet_connection()
        policy(settings={"devicesApprovalOn": False})

        (finding,) = raised("devices-join-without-approval")

        self.assertIn("Device approval", steps_text(finding))
        self.assertEqual(finding["remedies"], [])


class PolicyAmendmentTests(TestCase):
    def test_empty_groups_leave_the_policy_and_rules_left_admitting_nobody_go(self):
        amended, summary = policy_without_empty_groups(document())

        parsed = json.loads(amended)
        self.assertEqual(parsed["groups"], {"group:admins": ["someone@example.com"]})
        self.assertEqual(
            parsed["grants"],
            [{"src": ["group:admins"], "dst": ["tag:server"], "ip": ["tcp:22"]}],
        )
        self.assertEqual(parsed["ssh"], [])
        self.assertIn("group:empty", summary)
        self.assertIn("2 rules", summary)

    def test_nothing_to_remove_proposes_nothing(self):
        self.assertEqual(
            policy_without_empty_groups(json.dumps({"groups": {"group:a": ["x@example.com"]}})),
            ("", ""),
        )

    def test_a_group_named_elsewhere_is_refused(self):
        with self.assertRaisesRegex(ValueError, "tagOwners"):
            policy_without_empty_groups(
                document(tagOwners={"tag:server": ["group:empty"]})
            )

    def test_a_prefix_of_another_name_is_not_a_reference(self):
        amended, _ = policy_without_empty_groups(
            document(tagOwners={"tag:server": ["group:empty-but-other"]})
        )

        self.assertTrue(amended)


@override_settings(SEVERINO_MCP_ENABLE_INFRASTRUCTURE=True)
class RemoveEmptyGroupsCapabilityTests(TestCase):
    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        declare_policy(document())

    def test_a_token_proposes_and_a_person_must_agree(self):
        result = execute_capability(
            "tailnet.policy.remove_empty_groups",
            {"idempotency_key": "once", "reason": "Empty group."},
            principal=mcp_principal(),
            target=POLICY_KEY,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "awaiting_approval")
        stored = json.loads(ManagedResource.objects.get(key=POLICY_KEY).spec["document"])
        self.assertIn("group:empty", stored["groups"])

    def test_a_signed_in_operator_amends_the_declaration(self):
        result = request_empty_groups_removal(
            None, principal=an_operator(), current_key=POLICY_KEY
        )

        stored = json.loads(ManagedResource.objects.get(key=POLICY_KEY).spec["document"])
        self.assertNotIn("group:empty", stored["groups"])
        self.assertIn("group:empty", result["change"])

    def test_a_reader_is_refused(self):
        with self.assertRaises(AuthorizationError):
            request_empty_groups_removal(None, principal=READER, current_key=POLICY_KEY)

    def test_a_policy_with_no_empty_group_is_refused(self):
        operator = an_operator()
        request_empty_groups_removal(None, principal=operator, current_key=POLICY_KEY)

        with self.assertRaisesRegex(PolicyError, "no empty group"):
            request_empty_groups_removal(None, principal=operator, current_key=POLICY_KEY)

    def test_a_drifted_policy_is_refused_so_a_live_change_is_not_overwritten(self):
        """The live policy gained something (a connector's grant); editing HQ's
        stale copy and applying it would take that away with the fix."""

        policy = ManagedResource.objects.get(key=POLICY_KEY)
        policy.conditions = [{"type": "Drifted", "status": True, "reason": "Drifted", "message": "differs"}]
        policy.save(update_fields=["conditions"])
        before = policy.spec["document"]

        with self.assertRaisesRegex(PolicyError, "Keep the live version"):
            request_empty_groups_removal(None, principal=an_operator(), current_key=POLICY_KEY)
        self.assertEqual(ManagedResource.objects.get(key=POLICY_KEY).spec["document"], before)

    def test_an_unknown_or_other_kind_of_key_is_not_found(self):
        ManagedResource.objects.create(
            key="example-rewrite", kind="adguard.rewrite",
            spec={"domain": "a.example.com", "answer": "192.0.2.1"},
        )
        for key in ("missing", "example-rewrite"):
            with self.subTest(key=key), self.assertRaises(NotFoundError):
                request_empty_groups_removal(None, principal=cli_principal(), current_key=key)


class ProductionFindingsTests(TestCase):
    def test_an_empty_group_offers_the_policy_change_when_a_policy_is_declared(self):
        tailnet_connection()
        policy(
            groups=[{"name": "group:empty", "members": []}],
            grants=[{"src": ["group:empty"], "dst": ["tag:server"], "ip": ["tcp:22"]}],
        )
        (without,) = raised("empty-group-granted")
        declare_policy(document())

        (finding,) = raised("empty-group-granted")

        self.assertEqual(without["remedies"], [])
        self.assertIn("group:empty", steps_text(without))
        (remedy,) = finding["remedies"]
        self.assertEqual(remedy["capability"], "tailnet.policy.remove_empty_groups")
        self.assertEqual(remedy["target"], POLICY_KEY)

    @override_settings(SEVERINO_TRUSTED_NETWORKS=["100.64.0.0/10", "fd7a:115c:a1e0::/48"])
    def test_trusted_wider_says_the_exact_value_to_set(self):
        tailnet_connection()
        store(
            "tailscale.device",
            {"name": "example-host", "addresses": ["100.64.0.9"],
             "enabled_routes": ["192.0.2.0/24"]},
        )

        (finding,) = raised("trusted-wider-than-tailnet")

        self.assertIn(
            "fd7a:115c:a1e0::/48,100.64.0.9/32,192.0.2.0/24",
            steps_text(finding),
        )

    def test_a_stray_container_says_how_to_remove_it(self):
        from hq.domains.control_plane.models import ProviderConnection
        from django.utils import timezone

        ProviderConnection.objects.create(
            connection_ref="example-portainer", controller_id="example-controller",
            provider="portainer", endpoint="", observed_at=timezone.now(),
        )
        store(
            CONTAINER_KIND,
            {"name": "example-leftover", "host": "example-host", "stack": "",
             "state": "running", "connection_ref": "example-portainer"},
        )

        (finding,) = raised("unrecognised-container")

        self.assertIn("docker rm -f example-leftover", steps_text(finding))
        self.assertTrue(any(remedy["label"] == "Adopt it" for remedy in finding["remedies"]))


class FindingsPageTests(TestCase):
    def test_the_page_says_what_to_do_by_hand(self):
        from django.contrib.auth import get_user_model
        from django.urls import reverse

        user = get_user_model().objects.create_user("fix-op", password="x" * 20)
        self.client.force_login(user)
        tailnet_connection()
        policy(settings={"devicesApprovalOn": False})

        response = self.client.get(reverse("control_plane:findings"))

        self.assertContains(response, '<div class="operator-step">')
        self.assertContains(response, "turn on Device approval")


class DriftOffersBothWaysTests(TestCase):
    """Drift means something changed the live record outside HQ. Reconciling
    alone would undo that change, so it is never the only way out."""

    def node(self, status_label: str):
        from ..topology_model import TopologyNode

        return TopologyNode(
            id=f"resource:{POLICY_KEY}", kind="resource", label=POLICY_KEY,
            subtitle="", status="serious", status_label=status_label,
            kind_key="tailscale.policy",
        )

    def test_drift_leads_with_keeping_the_live_version(self):
        from ..controller_findings import _fault_remedies
        from ..infrastructure import DRIFT_LABEL

        remedies = _fault_remedies(self.node(DRIFT_LABEL))

        self.assertEqual(remedies[0].capability, "infrastructure.resource.accept_observed")
        self.assertEqual(remedies[0].label, "Keep the live version")
        self.assertEqual(
            [(r.capability, r.label) for r in remedies[1:]],
            [("infrastructure.reconcile", "Restore HQ's version")],
        )

    def test_another_fault_keeps_reconcile_alone(self):
        from ..controller_findings import _fault_remedies

        remedies = _fault_remedies(self.node("Needs attention"))

        self.assertNotIn(
            "infrastructure.resource.accept_observed", [r.capability for r in remedies]
        )


class KeepTheLiveVersionPageTests(TestCase):
    """The way to keep a live change is reachable, preselected and plainly worded."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        declare_policy(document())
        user = get_user_model().objects.create_user(
            "keep-op", password="x" * 20, is_staff=True, is_superuser=True
        )
        self.client.force_login(user)

    def drift(self):
        policy = ManagedResource.objects.get(key=POLICY_KEY)
        policy.conditions = [{"type": "Drifted", "status": True, "reason": "Drifted", "message": "differs"}]
        policy.save(update_fields=["conditions"])

    def test_a_target_past_the_first_page_is_listed_and_preselected(self):
        from django.urls import reverse

        # Keys that sort before the policy's, more than one default page of them.
        ManagedResource.objects.bulk_create(
            ManagedResource(
                key=f"a-{index:03}", kind="adguard.rewrite",
                spec={"domain": f"h{index}.example.com", "answer": "192.0.2.1"},
            )
            for index in range(60)
        )

        response = self.client.get(
            reverse("command", kwargs={"name": "infrastructure.resource.accept_observed"}),
            {"target": POLICY_KEY},
        )

        self.assertContains(response, f'<option value="{POLICY_KEY}" selected>')
        self.assertContains(response, "Keep the live version")
        # It takes a key and a reason, no record: nothing it could blank.
        self.assertNotContains(response, "This replaces the whole record")

    def test_a_drifted_resource_page_offers_both_ways(self):
        from django.urls import reverse

        self.drift()

        response = self.client.get(reverse("control_plane:detail", args=[POLICY_KEY]))

        content = response.content.decode()
        self.assertIn("Keep the live version", content)
        self.assertIn("Restore HQ&#x27;s version", content)
        self.assertLess(
            content.index("Keep the live version"), content.index("Restore HQ&#x27;s version")
        )

    def test_a_resource_in_step_offers_neither(self):
        from django.urls import reverse

        response = self.client.get(reverse("control_plane:detail", args=[POLICY_KEY]))

        self.assertNotContains(response, "Keep the live version")
        self.assertNotContains(response, "Restore HQ&#x27;s version")
