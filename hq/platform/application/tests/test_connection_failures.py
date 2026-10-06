"""A connection that does not answer: its cause is stored at the source and its
fix follows the cause."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ProviderConnection

from ..credential_mint import address_fields
from ..findings import findings
from ..report_testing import report_connections
from ..security import Capability, Principal, cli_principal

EVERYTHING = Principal("test", "operator", frozenset(Capability))
SIGN_IN = (
    "The address answered with a sign-in page at sso.example.com, not the API. "
    "Use the provider's direct API address."
)


def report(**connection):
    report_connections(
        [
            {
                "connection_ref": "example-npm",
                "provider": "npm",
                "endpoint": "https://proxy.example.com",
                "ok": False,
                **connection,
            }
        ],
        principal=cli_principal(),
        controller_id="example-controller",
    )


def finding():
    (found,) = findings(principal=EVERYTHING, rule="connection-not-answering")["findings"]
    return found


class StoredFailureTests(TestCase):
    def stored(self):
        return ProviderConnection.objects.get(connection_ref="example-npm").failure

    def test_the_reported_cause_is_stored(self):
        report(detail=SIGN_IN, failure="address")

        self.assertEqual(self.stored(), "address")

    def test_a_connection_that_answers_carries_no_cause(self):
        report(ok=True, detail="Answered", failure="address")

        self.assertEqual(self.stored(), "")


class FixFollowsCauseTests(TestCase):
    def test_a_sign_in_page_asks_for_the_direct_address_in_the_item_field(self):
        report(detail=SIGN_IN, failure="address")

        found = finding()

        self.assertEqual(found["title"], "example-npm does not answer as its API")
        (step,) = found["operator_steps"]
        self.assertEqual(
            step["label"], "Point the connection at the provider's direct API address"
        )
        self.assertIn(address_fields(), " ".join(step["notes"]))
        self.assertIn("https://proxy.example.com does not answer as the API.", step["notes"])
        self.assertIn({"label": "Cause", "value": "The address is not the API"}, found["evidence"])
        self.assertNotIn("credential", step["label"].lower())

    def test_no_answer_asks_to_check_the_machine_and_route(self):
        report(detail="Provider request failed: URLError.", failure="network")

        (step,) = finding()["operator_steps"]

        self.assertEqual(
            step["label"],
            "Check that proxy.example.com is up and the controller has a route to it",
        )

    def test_a_refused_credential_asks_for_a_replacement(self):
        report(detail="Provider request failed: HTTPError.", failure="credential")

        (step,) = finding()["operator_steps"]

        self.assertIn("Replace the credential", step["label"])

    def test_an_unclassified_failure_falls_back_to_the_rule(self):
        report(detail="Something else")

        (step,) = finding()["operator_steps"]

        self.assertIn("Fix what the connection's error names", step["label"])

    def test_the_page_leads_with_the_cause_specific_fix(self):
        report(detail=SIGN_IN, failure="address")
        user = get_user_model().objects.create_user("operator", password="pw", is_staff=True)
        self.client.force_login(user)

        response = self.client.get(reverse("control_plane:findings"))

        self.assertContains(response, "Point the connection at the provider&#x27;s direct API address")
        self.assertNotContains(response, "Fix or replace the connection")


class SshTransportTests(TestCase):
    def test_a_host_and_port_endpoint_names_the_host(self):
        ProviderConnection.objects.create(
            connection_ref="example-ssh", controller_id="example-controller",
            provider="ssh", endpoint="192.0.2.9:22", reachable=False, probed=True,
            detail="Timed out", failure="network", observed_at=timezone.now(),
        )

        (step,) = finding()["operator_steps"]

        self.assertEqual(
            step["label"], "Check that 192.0.2.9 is up and the controller has a route to it"
        )


class FindingCardTests(TestCase):
    """The card leads with the finding's own fix; impact and confirm are one line."""

    def setUp(self):
        report(detail=SIGN_IN, failure="address")
        user = get_user_model().objects.create_user("operator", password="pw", is_staff=True)
        self.client.force_login(user)

    def card(self):
        page = self.client.get(reverse("control_plane:findings")).content.decode()
        start = page.index('class="card finding-card')
        return page[start : page.index("</article>", start)]

    def test_the_specific_step_comes_first_and_the_generic_plan_is_gone(self):
        card = self.card()

        self.assertLess(card.index("operator-step"), card.index("finding-links"))
        self.assertNotIn("What to do", card)
        self.assertNotIn("Fix it", card)
        self.assertNotIn("resolution-workflow", card)
        # A connection that does not answer is a reading: confirming it reads now.
        self.assertIn("Check again", card)
        self.assertIn('formaction="/infrastructure/connections/read/?connection_ref=example-npm', card)

    def test_open_connections_is_a_link_not_the_fix(self):
        card = self.card()
        fix = card[card.index('class="finding-fix"') : card.index("finding-links")]

        self.assertNotIn("Open connections", fix)

    def test_the_api_shape_is_unchanged(self):
        found = finding()

        self.assertIn("operator_steps", found)
        self.assertIn("workflow", found)
        self.assertEqual(
            [step["phase"] for step in found["workflow"]["steps"]][-1], "verify"
        )
