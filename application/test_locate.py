"""Turning an address into a machine, and the four ways that used to disagree.

Every surface that draws a line between two things HQ knows -- a proxy and the
box it forwards to, a credential and the machine it opens, a service and where
it runs -- is asking one question. It was answered in four places, and the four
did not agree: one handled loopback, one consulted credentials, one read only
declarations, one intersected sets of strings. So the same address named a
machine on one page and nothing on the next.

These are the cases that used to come out differently depending on which page
asked, plus the ones the shared parser exists for.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from control_plane.models import ManagedResource, ProviderConnection

from .locate import index_of, machines_index, split_endpoint
from .services import Origin, _locate, _machines


def a_machine(key, name, *addresses, role=""):
    return ManagedResource.objects.create(
        key=key,
        kind="machine",
        spec={"name": name, "role": role, "addresses": list(addresses)},
    )


def a_proxy(key, hostname, host, port=8000):
    return ManagedResource.objects.create(
        key=key,
        kind="npm.proxy_host",
        spec={
            "domain_names": [hostname],
            "forward_scheme": "http",
            "forward_host": host,
            "forward_port": port,
            "connection_ref": "an-npm",
        },
    )


def a_connection(ref, provider, endpoint="", reaches=()):
    return ProviderConnection.objects.create(
        connection_ref=ref,
        controller_id="a-controller",
        provider=provider,
        endpoint=endpoint,
        reaches=list(reaches),
        reachable=True,
        probed=True,
        observed_at=timezone.now(),
    )


class EndpointParsingTests(TestCase):
    """One parser, because ``rpartition(":")`` is wrong for two of these.

    Five call sites read endpoints by splitting at the last colon. That is
    correct for ``10.0.0.5:8000`` and wrong for everything IPv6: a bare address
    is full of colons and has no port at all, and a bracketed one keeps its
    brackets in the host.
    """

    def test_a_host_and_a_port_split(self):
        self.assertEqual(split_endpoint("10.0.0.5:8000"), ("10.0.0.5", "8000"))

    def test_an_address_with_no_port_is_all_host(self):
        self.assertEqual(split_endpoint("10.0.0.5"), ("10.0.0.5", ""))

    def test_a_bare_ipv6_address_is_not_split_at_its_last_colon(self):
        self.assertEqual(split_endpoint("2001:db8::5"), ("2001:db8::5", ""))

    def test_a_bracketed_ipv6_endpoint_keeps_neither_bracket(self):
        self.assertEqual(split_endpoint("[2001:db8::5]:8000"), ("2001:db8::5", "8000"))

    def test_a_url_is_read_as_the_host_it_names(self):
        self.assertEqual(
            split_endpoint("https://control.example:9443/api"),
            ("control.example", "9443"),
        )

    def test_nothing_parses_to_nothing_rather_than_to_a_guess(self):
        self.assertEqual(split_endpoint(""), ("", ""))


class NamespaceTests(TestCase):
    """A machine's name and a machine's address are different kinds of fact.

    Kept in one dictionary they overwrite each other silently, and which one
    survives depends on the order rows came back from the database.
    """

    def setUp(self):
        a_machine("example-alpha", "example-alpha", "10.0.0.5")

    def test_a_machine_named_like_an_address_does_not_take_that_address(self):
        """Both facts survive, and neither answers for the other."""

        a_machine("example-oddly-named", "10.0.0.5")

        index = machines_index()
        self.assertEqual(index.named("10.0.0.5"), "10.0.0.5")
        self.assertEqual(index.at("10.0.0.5"), "example-alpha")

    def test_a_machine_named_like_an_address_is_still_its_own_machine(self):
        from .machines import machine_catalog

        a_machine("example-oddly-named", "10.0.0.5")

        self.assertEqual(
            sorted(item.name for item in machine_catalog()),
            ["10.0.0.5", "example-alpha"],
        )

    def test_a_credential_named_like_an_address_does_not_claim_it(self):
        """The realistic version, and the one that was wrong.

        A connection's ref is a name. Filed into the same map as addresses it
        took ownership of the machine that actually answers there, so a proxy
        forwarding to that address was reported under a credential.
        """

        a_connection("10.0.0.5", "cloudflare_dns", endpoint="https://api.example")

        self.assertEqual(machines_index().at("10.0.0.5"), "example-alpha")

    def test_an_address_is_matched_as_an_address_and_not_as_a_name(self):
        self.assertEqual(machines_index().named("10.0.0.5"), "")


class ForwardingAddressTests(TestCase):
    """What a proxy's forwarding address resolves to, and when it resolves to nothing."""

    def setUp(self):
        a_machine("example-alpha", "example-alpha", "10.0.0.5", role="Workstation")

    def test_a_declared_address_names_the_machine(self):
        self.assertEqual(_locate("10.0.0.5:8000", _machines()).host, "example-alpha")

    def test_an_address_nothing_claims_names_nothing(self):
        origin = _locate("198.51.100.9:8000", _machines())

        self.assertEqual(origin.host, "")
        self.assertEqual(origin.address, "198.51.100.9:8000")

    def test_a_bracketed_ipv6_forward_is_matched_rather_than_truncated(self):
        a_machine("example-six", "example-six", "2001:db8::5")

        self.assertEqual(
            _locate("[2001:db8::5]:8000", _machines()).host, "example-six"
        )

    def test_a_bare_ipv6_answer_is_somewhere_else_rather_than_an_ingress(self):
        """No port means a DNS record named it, which counting colons got wrong."""

        self.assertTrue(Origin(address="2001:db8::5").external)

    def test_a_bracketed_ipv6_ingress_is_not_somewhere_else(self):
        self.assertFalse(Origin(address="[2001:db8::5]:8000").external)

    def test_a_machine_only_a_credential_names_still_answers(self):
        """A credential that opens a shell somewhere is that somewhere."""

        a_connection("example-edge", "ssh", endpoint="198.51.100.7:22")

        self.assertEqual(
            _locate("198.51.100.7:8000", _machines()).host, "example-edge"
        )


class OneMachineOneRowTests(TestCase):
    """A declared machine and the credential that opens it are one machine.

    Aliasing compared a connection's endpoint against container sweeps and
    nothing else, so a machine HQ had been told about and the SSH item reaching
    it sat side by side as two rows -- the declaration holding the role and the
    address, the credential holding everything served from there.
    """

    def setUp(self):
        a_machine("example-alpha", "example-alpha", "10.0.0.5", role="Workstation")
        a_connection("example-shell", "ssh", endpoint="10.0.0.5:22")
        a_proxy("example-proxy", "dev.example.com", "10.0.0.5")

    def catalog(self):
        from .machines import machine_catalog

        return machine_catalog()

    def test_the_credential_does_not_become_a_second_machine(self):
        self.assertEqual([item.name for item in self.catalog()], ["example-alpha"])

    def test_the_credential_is_kept_as_an_alias(self):
        self.assertEqual(self.catalog()[0].aliases, ("example-shell",))

    def test_what_is_served_from_there_is_filed_under_the_same_machine(self):
        """The board and the service page used to name different machines."""

        from .machines import machine

        self.assertEqual(machine("example-alpha").hostnames, ("dev.example.com",))


class ConnectionPageTests(TestCase):
    """The page whose subject is what HQ can reach, naming what it reaches."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="an-operator", password="not-a-real-password"
        )
        self.client.force_login(self.user)
        a_machine("example-alpha", "example-alpha", "10.0.0.5")

    def page(self):
        return self.client.get(reverse("control_plane:connections"))

    def test_a_credential_opening_a_declared_machine_names_it(self):
        a_connection("example-shell", "ssh", endpoint="10.0.0.5:22")

        response = self.page()
        self.assertContains(response, "example-alpha")
        self.assertContains(
            response, reverse("control_plane:machine", kwargs={"name": "example-alpha"})
        )

    def test_a_credential_pointing_at_a_declared_machine_names_it(self):
        """A URL endpoint is a service on a machine, and HQ knows whose."""

        a_connection(
            "example-dns", "cloudflare_dns",
            endpoint="https://10.0.0.5/api", reaches=["example.com"],
        )

        self.assertContains(self.page(), "example-alpha")

    def test_a_credential_pointing_nowhere_hq_knows_still_shows_its_endpoint(self):
        a_connection("example-far", "ssh", endpoint="198.51.100.9:22")

        response = self.page()
        self.assertContains(response, "198.51.100.9:22")
        self.assertNotContains(response, "example-alpha")


class ProxyPageTests(TestCase):
    """A forwarding address, named where HQ can and printed where it cannot."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="an-operator", password="not-a-real-password"
        )
        self.client.force_login(self.user)
        a_machine("example-alpha", "example-alpha", "10.0.0.5", role="Workstation")

    def board(self):
        return self.client.get(reverse("control_plane:list"))

    def test_the_board_names_the_machine_a_proxy_forwards_to(self):
        a_proxy("example-proxy", "dev.example.com", "10.0.0.5")

        self.assertContains(
            self.board(),
            reverse("control_plane:machine", kwargs={"name": "example-alpha"}),
        )

    def test_the_board_still_prints_an_address_it_cannot_place(self):
        a_proxy("example-proxy", "dev.example.com", "198.51.100.9")

        response = self.board()
        self.assertContains(response, "198.51.100.9")
        self.assertNotContains(
            response,
            reverse("control_plane:machine", kwargs={"name": "example-alpha"}),
        )

    def test_the_resource_page_names_it_too(self):
        a_proxy("example-proxy", "dev.example.com", "10.0.0.5")

        response = self.client.get(
            reverse("control_plane:detail", kwargs={"key": "example-proxy"})
        )
        self.assertContains(response, "example-alpha")
        # Still said out loud, because the address is what nginx was configured
        # with and the name is HQ's reading of it.
        self.assertContains(response, "10.0.0.5")

    def test_naming_every_row_costs_no_more_than_naming_one(self):
        """Read once for the page. Per row it grows with the estate.

        Asserted as a comparison rather than against a number, so the guard is
        about the shape of the cost and survives an unrelated query being added
        to the page.
        """

        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        a_proxy("example-proxy-0", "a0.example.com", "10.0.0.5")
        with CaptureQueriesContext(connection) as one_row:
            self.board()
        for index in range(1, 7):
            a_proxy(f"example-proxy-{index}", f"a{index}.example.com", "10.0.0.5")
        with CaptureQueriesContext(connection) as seven_rows:
            self.board()

        self.assertEqual(len(seven_rows), len(one_row))


class EvidenceTests(TestCase):
    """The index resolves what it was handed, and never goes looking.

    Which is what lets the connection panel -- rendered on every page, having
    already read the declarations -- use the same resolver as the machine board
    without paying the board's queries.
    """

    def test_declarations_alone_answer_without_a_query(self):
        declared = ({"name": "example-alpha", "addresses": ["10.0.0.5"]},)

        with self.assertNumQueries(0):
            self.assertEqual(index_of(declared=declared).at("10.0.0.5:8000"),
                             "example-alpha")

    def test_a_declaration_outranks_a_credential_at_the_same_address(self):
        """Most deliberate first, so one machine does not become two rows."""

        a_machine("example-alpha", "example-alpha", "10.0.0.5")
        a_connection("example-shell", "ssh", endpoint="10.0.0.5:22")

        self.assertEqual(machines_index().at("10.0.0.5"), "example-alpha")

    def test_a_name_and_an_address_reach_the_same_machine(self):
        a_machine("example-alpha", "example-alpha", "10.0.0.5")

        index = machines_index()
        self.assertEqual(index.resolve("example-alpha:8000"), "example-alpha")
        self.assertEqual(index.resolve("10.0.0.5:8000"), "example-alpha")

    def test_the_inverse_direction_reads_the_same_pair(self):
        """A form filling in an address and a page reading one cannot disagree."""

        a_machine("example-alpha", "example-alpha", "10.0.0.5")

        self.assertEqual(machines_index().address_for("example-alpha"), "10.0.0.5")
