"""A connection that does not answer: its cause is stored at the source and its
fix follows the cause."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ProviderConnection

from ..credential_mint import address_fields
from ..findings import findings
from ..report_testing import report_connections
from ..security import Capability, Principal, cli_principal
from .markup import select, within

EVERYTHING = Principal("test", "operator", frozenset(Capability))
SIGN_IN = (
    "The address answered with a sign-in page at sso.example.com, not the API. Use the provider's direct API address."
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

        self.assertEqual(found["title"], "example-npm: the address is not Nginx Proxy Manager's API")
        (step,) = found["operator_steps"]
        self.assertEqual(step["label"], "Point the connection at Nginx Proxy Manager's own API address")
        self.assertIn(address_fields(), " ".join(step["notes"]))
        self.assertIn("https://proxy.example.com is not the API.", step["notes"])
        self.assertIn({"label": "Cause", "value": "The address is not the API"}, found["evidence"])
        self.assertNotIn("credential", step["label"].lower())

    def test_no_answer_asks_to_check_the_machine_and_route(self):
        report(detail="Provider request failed: URLError.", failure="network")

        (step,) = finding()["operator_steps"]

        self.assertEqual(
            step["label"],
            "Check that proxy.example.com is up and the controller can reach it",
        )

    def test_a_refused_credential_asks_for_a_replacement(self):
        report(detail="Provider request failed: HTTPError.", failure="credential")

        (step,) = finding()["operator_steps"]

        self.assertIn("Replace the credential", step["label"])

    def test_an_unclassified_failure_falls_back_to_the_rule(self):
        report(detail="Something else")

        (step,) = finding()["operator_steps"]

        self.assertIn("fix what its error says", step["label"])

    def test_the_page_leads_with_the_cause_specific_fix(self):
        report(detail=SIGN_IN, failure="address")
        user = get_user_model().objects.create_user("operator", password="pw", is_staff=True)
        self.client.force_login(user)

        response = self.client.get(reverse("control_plane:findings"))

        self.assertContains(response, "Point the connection at Nginx Proxy Manager&#x27;s own API address")
        self.assertNotContains(response, "Fix or replace the connection")


class SshTransportTests(TestCase):
    def test_a_host_and_port_endpoint_names_the_host(self):
        ProviderConnection.objects.create(
            connection_ref="example-ssh",
            controller_id="example-controller",
            provider="ssh",
            endpoint="192.0.2.9:22",
            reachable=False,
            probed=True,
            detail="Timed out",
            failure="network",
            observed_at=timezone.now(),
        )

        (step,) = finding()["operator_steps"]

        self.assertEqual(step["label"], "Check that 192.0.2.9 is up and the controller can reach it")


class FindingCardTests(TestCase):
    """A finding is the queue's own card: its fix, its buttons, then its links."""

    def setUp(self):
        report(detail=SIGN_IN, failure="address")
        user = get_user_model().objects.create_user("operator", password="pw", is_staff=True)
        self.client.force_login(user)

    def card(self):
        """The first card's words, and what it holds in page order."""

        page = self.client.get(reverse("control_plane:findings"))
        card = select(page, "data-attention-item")[0]
        return card.words, [element for element in within(page, "data-attention-item") if card in element.ancestors]

    def test_the_specific_step_comes_first_and_the_generic_plan_is_gone(self):
        words, held = self.card()

        parts = [element for element in held if element.classes & {"attention-body", "attention-links"}]
        self.assertEqual(
            [part.classes & {"attention-body", "attention-links"} for part in parts][:2],
            [
                {"attention-body"},
                {"attention-links"},
            ],
        )
        self.assertNotIn("What to do", words)
        self.assertNotIn("Fix it", words)
        # A connection that does not answer is a reading: confirming it reads now.
        self.assertIn("Check again", words)
        self.assertTrue(
            any(
                element.attrs.get("formaction", "").startswith(
                    "/infrastructure/connections/read/?connection_ref=example-npm"
                )
                for element in held
            )
        )

    def test_open_connections_is_a_link_not_the_fix(self):
        words, held = self.card()
        (links,) = [element for element in held if "attention-links" in element.classes]

        self.assertIn("Open connections", links.words)
        self.assertEqual(words.count("Open connections"), 1)

    def test_the_api_shape_is_unchanged(self):
        found = finding()

        self.assertIn("operator_steps", found)
        self.assertIn("workflow", found)
        self.assertEqual([step["phase"] for step in found["workflow"]["steps"]][-1], "verify")
