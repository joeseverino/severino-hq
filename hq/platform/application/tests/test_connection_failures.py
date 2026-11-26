"""A connection that does not answer: its cause is stored at the source and its
fix follows the cause."""

from django.test import TestCase
from django.urls import reverse

from hq.domains.control_plane.models import ProviderConnection
from hq.platform.application.markup_testing import select, within
from hq.platform.core.testing import connection, operator

from ..credential_mint import address_fields
from ..findings import findings
from ..report_testing import report_connections
from ..security import Capability, Principal

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
        (step,) = found["steps"]
        self.assertEqual(step["text"], "Point the connection at Nginx Proxy Manager's own API address")
        self.assertIn(address_fields(), " ".join(step["notes"]))
        self.assertIn("https://proxy.example.com is not the API.", step["notes"])
        self.assertIn(
            {"label": "Cause", "value": "The address is not the API", "url": "", "code": False}, found["evidence"]
        )
        self.assertNotIn("credential", step["text"].lower())

    def test_no_answer_asks_to_check_the_machine_and_route(self):
        report(detail="Provider request failed: URLError.", failure="network")

        (step,) = finding()["steps"]

        self.assertEqual(
            step["text"],
            "Check that proxy.example.com is up and the controller can reach it",
        )

    def test_a_refused_credential_asks_for_a_replacement(self):
        report(detail="Provider request failed: HTTPError.", failure="credential")

        (step,) = finding()["steps"]

        self.assertIn("Replace the credential", step["text"])

    def test_an_unclassified_failure_falls_back_to_the_rule(self):
        report(detail="Something else")

        (step,) = finding()["steps"]

        self.assertIn("fix what its error says", step["text"])

    def test_the_page_leads_with_the_cause_specific_fix(self):
        report(detail=SIGN_IN, failure="address")
        operator(self.client)

        response = self.client.get(reverse("control_plane:findings"))

        self.assertContains(response, "Point the connection at Nginx Proxy Manager&#x27;s own API address")
        self.assertNotContains(response, "Fix or replace the connection")


class SshTransportTests(TestCase):
    def test_a_host_and_port_endpoint_names_the_host(self):
        connection(
            "ssh", "example-ssh", endpoint="192.0.2.9:22", reachable=False, detail="Timed out", failure="network"
        )

        (step,) = finding()["steps"]

        self.assertEqual(step["text"], "Check that 192.0.2.9 is up and the controller can reach it")


class FindingCardTests(TestCase):
    """A finding is the queue's own card: its fix, its buttons, then its links."""

    def setUp(self):
        report(detail=SIGN_IN, failure="address")
        operator(self.client)

    def card(self):
        """The first card's words, and what it holds in page order, the
        blocks it keeps on its open line included."""

        page = self.client.get(reverse("control_plane:findings"))
        card = select(page, "data-attention-item")[0]
        held = [element for element in within(page, "data-attention-item") if element.inside(card)]
        below = [element.words for element in held if "row-below" in element.classes]
        return " ".join((card.words, *below)), held

    def test_the_specific_step_comes_first_and_the_generic_plan_is_gone(self):
        words, held = self.card()

        parts = [
            "links" if "data-attention-links" in element.attrs else "gutter"
            for element in held
            if "row-gutter" in element.classes or "data-attention-links" in element.attrs
        ]
        self.assertEqual(parts[:2], ["gutter", "links"])
        self.assertNotIn("Next steps", words)
        self.assertNotIn("Fix it", words)

    def test_open_connections_is_a_link_not_the_fix(self):
        words, held = self.card()
        (links,) = [element for element in held if "data-attention-links" in element.attrs]

        self.assertIn("Open connections", links.words)
        self.assertEqual(words.count("Open connections"), 1)

    def test_the_api_shape_is_unchanged(self):
        found = finding()

        self.assertIn("steps", found)
