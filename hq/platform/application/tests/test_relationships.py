"""Entity pages read one relation graph, and every name links one way."""

import re
from html.parser import HTMLParser

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, ProviderConnection, ProviderInventory
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.providers import PROVIDERS
from hq.platform.core.tests.estate_sizes import (
    assert_cost_does_not_grow,
    assert_subject_cost_does_not_grow,
    distinct,
)

from ..entity_links import NODE_KINDS, EntityLink, entity_link, kind_label
from ..facts import Subject, readings, unreadable_labels
from ..projection import projection_scope
from ..relationships import relationships_for
from ..security import Capability, Principal
from ..services import service_or_prospect
from ..tailnet import TAILNET_KIND
from ..topology import relation_graph
from ..topology_model import READING_RANKS, RELATIONS
from .markup import hrefs, text
from .test_hq_self import SITE, declare, own

READER = Principal("reader", "test", frozenset({Capability.READ}))
CONTROLLER = "example-controller"
ACCOUNT = "0123456789abcdef"


def store(kind, *records, reachable=True, connected=True, error=""):
    ProviderInventory.objects.update_or_create(
        kind=kind,
        defaults={
            "records": list(records),
            "reachable": reachable,
            "connected": connected,
            "error": error,
            "controller_id": CONTROLLER,
            "observed_at": timezone.now(),
        },
    )


def connected(ref, provider):
    return ProviderConnection.objects.create(
        connection_ref=ref,
        controller_id=CONTROLLER,
        provider=provider,
        reaches=[],
        reachable=True,
        probed=True,
        observed_at=timezone.now(),
    )


def dns(name, content, *, proxied, record_type="A"):
    return {
        "zone": "example.com",
        "name": name,
        "record_type": record_type,
        "content": content,
        "proxied": proxied,
        "ttl": 1,
    }


def login(client, name="relations-op"):
    user = get_user_model().objects.create_user(name, password="x" * 20)
    client.force_login(user)


@SITE
class OneEdgeBothEndsTests(TestCase):
    """The machine running HQ and HQ's own service read one edge."""

    def setUp(self):
        declare("example-host", "192.0.2.44")
        login(self.client)

    def test_the_machine_serves_it_and_it_runs_on_the_machine(self):
        with own("192.0.2.44"), projection_scope():
            graph = relation_graph(principal=READER)
            machine = relationships_for("machine:example-host", principal=READER)
            service = relationships_for("service:hq.example.com", principal=READER)

        edges = [
            edge
            for edge in graph.topology.edges
            if {edge.source, edge.target} == {"service:hq.example.com", "machine:example-host"}
        ]
        self.assertEqual([edge.kind for edge in edges], ["runs_on"])
        self.assertEqual(machine.labels("Serves"), ("hq.example.com",))
        self.assertEqual(service.labels("Runs on"), ("example-host",))
        (item,) = service.group("Runs on").items
        self.assertEqual(item.entity.url, reverse("control_plane:machine", args=["example-host"]))

    def test_both_pages_render_it(self):
        with own("192.0.2.44"):
            machine = self.client.get(reverse("control_plane:machine", args=["example-host"]))
            service = self.client.get(reverse("control_plane:service", args=["hq.example.com"]))

        # The machine end is "Names it answers", which lists what it serves.
        self.assertIn("/infrastructure/services/hq.example.com/", hrefs(machine))
        # The service end is its path, which names the machine hop by hop, and
        # the trace in the topology sits beside it.
        self.assertIn("/infrastructure/machines/example-host/", hrefs(service))
        self.assertContains(service, "?focus=service%3Ahq.example.com")

    def test_hqs_own_service_is_read_only(self):
        with own("192.0.2.44"):
            response = self.client.get(reverse("control_plane:service", args=["hq.example.com"]))

        badges = {badge.label: badge for badge in response.context["page_badges"]}
        self.assertTrue(badges["Read-only"].title)
        # Nothing on the page offers to change it.
        self.assertEqual([action.label for action in response.context["page"].actions if action.method == "post"], [])
        self.assertContains(response, reverse("control_plane:machine", args=["example-host"]))


class LinkBuilderTests(TestCase):
    def test_every_node_kind_says_what_it_is(self):
        for kind in NODE_KINDS:
            with self.subTest(kind=kind):
                self.assertNotIn(".", kind_label(kind))
                self.assertNotEqual(kind_label(kind), kind)

    def test_registered_kinds_read_as_their_labels(self):
        for kind in (TAILNET_KIND, "cloudflare.dns_record", "cloudflare.access_app"):
            with self.subTest(kind=kind):
                declared = PROVIDERS[kind].label if kind in PROVIDERS else OBSERVATIONS[kind].label
                self.assertEqual(kind_label(kind), declared)

    def test_an_estate_kind_links_to_its_page(self):
        link = entity_link("machine", "example-host")

        self.assertEqual(link.url, reverse("control_plane:machine", args=["example-host"]))
        self.assertFalse(link.external)

    def test_a_connection_links_to_its_row(self):
        link = entity_link("connection", "example-cloudflare")

        self.assertEqual(link.url, reverse("control_plane:connections") + "#connection-example-cloudflare")

    def test_a_reading_links_to_its_console_from_stored_ids(self):
        pages = entity_link(
            "cloudflare.pages_project",
            "",
            record={"name": "example-site", "account_id": ACCOUNT},
        )
        unlinked = entity_link("cloudflare.pages_project", "", record={"name": "example-site"})

        self.assertEqual(pages.label, "example-site")
        self.assertEqual(pages.url, f"https://dash.cloudflare.com/{ACCOUNT}/pages/view/example-site")
        self.assertTrue(pages.external)
        self.assertEqual((unlinked.label, unlinked.url, unlinked.external), ("example-site", "", False))

    def test_a_tailnet_device_links_to_the_admin_console(self):
        link = entity_link(TAILNET_KIND, "example-host", record={"addresses": ["fd7a::1", "100.64.0.1"]})

        self.assertEqual(link.url, "https://login.tailscale.com/admin/machines/100.64.0.1")
        self.assertTrue(link.external)

    def test_a_declaration_links_to_its_page(self):
        link = entity_link("cloudflare.dns_record", "app-dns")

        self.assertEqual(link.url, reverse("control_plane:detail", args=["app-dns"]))

    def test_a_name_that_is_not_a_host_has_no_service_page(self):
        for name in ("sig1._domainkey.example.com", "_dmarc.example.com", "*.example.com"):
            with self.subTest(name=name):
                self.assertEqual(entity_link("service", name).url, "")

        response_url = reverse("control_plane:service", args=["sig1._domainkey.example.com"])
        login(self.client)
        self.assertEqual(self.client.get(response_url).status_code, 404)

    def test_an_unregistered_kind_is_refused(self):
        with self.assertRaises(KeyError):
            entity_link("example.unregistered", "x")

    def test_consoles_are_built_only_from_record_fields_the_schema_keeps(self):
        for kind, spec in OBSERVATIONS.items():
            with self.subTest(kind=kind):
                self.assertEqual(spec.console({}), "")

    def test_a_fronting_kind_is_one_a_provider_says_it_fronts(self):
        for kind, spec in OBSERVATIONS.items():
            if spec.fronted_by:
                with self.subTest(kind=kind):
                    self.assertIsNotNone(PROVIDERS[spec.fronted_by].fronts)


class RankTests(TestCase):
    def test_what_serves_comes_first_and_overlays_last(self):
        self.assertLess(RELATIONS["runs_on"].rank, READING_RANKS["certificate"])
        self.assertLess(READING_RANKS["runtime"], READING_RANKS["certificate"])
        self.assertEqual(max(READING_RANKS.values()), READING_RANKS[""])
        self.assertLess(RELATIONS["declared_by"].rank, READING_RANKS[""])


EDGE = {
    "connection_ref": "example-cloudflare",
    "account_id": ACCOUNT,
    "zone": "example.com",
    "id": "e1",
    "hosts": ["example.com", "*.example.com"],
    "status": "active",
    "certificate_authority": "google",
    "expires_on": "2099-01-01T00:00:00+00:00",
}


class EdgeCertificatePathTests(TestCase):
    """An edge certificate serves a name only through a proxied record."""

    def setUp(self):
        store("cloudflare.edge_certificate", EDGE)
        store(
            "cloudflare.dns_record",
            dns("app.example.com", "192.0.2.10", proxied=True),
            dns("tail.example.com", "100.64.0.1", proxied=False),
        )

    def about(self, hostname):
        return [item.kind for item in readings().about(Subject.of(hostnames=(hostname,)))]

    def test_a_proxied_name_is_covered(self):
        self.assertEqual(self.about("app.example.com"), ["cloudflare.edge_certificate"])

    def test_an_unproxied_name_or_one_no_record_fronts_is_not_although_the_wildcard_covers_it(self):
        for name in ("tail.example.com", "other.example.com"):
            with self.subTest(name):
                self.assertEqual(self.about(name), [])

    def test_the_domain_still_holds_its_certificates(self):
        found = readings().about(Subject.of(zones=("example.com",)), facets=("certificate",))

        self.assertEqual([item.record["id"] for item in found], ["e1"])

    def test_the_service_page_of_an_unproxied_name_names_no_edge_certificate(self):
        ManagedResource.objects.create(
            key="tail-dns",
            kind="adguard.rewrite",
            spec={"domain": "tail.example.com", "answer": "100.64.0.1"},
        )
        ManagedResource.objects.create(
            key="app-dns",
            kind="adguard.rewrite",
            spec={"domain": "app.example.com", "answer": "192.0.2.10"},
        )
        login(self.client)

        tail = self.client.get(reverse("control_plane:service", args=["tail.example.com"]))
        app = self.client.get(reverse("control_plane:service", args=["app.example.com"]))

        label = kind_label("cloudflare.edge_certificate")
        self.assertNotContains(tail, label)
        self.assertContains(app, label)


def wildcard(key):
    return ManagedResource.objects.create(
        key=key,
        kind="tls.certificate",
        spec={
            "certificate_name": key,
            "domains": ["*.example.com"],
            "install_on": ["a-proxy"],
            "renewal_window_days": 30,
        },
    )


class ManagedCertificatePathTests(TestCase):
    """A managed certificate applies where the ingress serving the name installs it."""

    def setUp(self):
        wildcard("wildcard-a")
        wildcard("wildcard-b")

    def proxy(self, certificate=""):
        return ManagedResource.objects.create(
            key="app-proxy",
            kind="npm.proxy_host",
            spec={
                "domain_names": ["app.example.com"],
                "forward_scheme": "http",
                "forward_host": "10.0.0.10",
                "forward_port": 8000,
                "certificate_resource": certificate,
            },
        )

    def certificates(self):
        found = service_or_prospect("app.example.com")
        facet = next(item for item in found.facets if item.id == "certificate")
        return sorted(claim.resource_key for claim in facet.claims)

    def test_the_certificate_the_ingress_names_is_the_one_that_applies(self):
        self.proxy("wildcard-b")

        self.assertEqual(self.certificates(), ["wildcard-b"])

    def test_an_ingress_naming_none_leaves_every_covering_certificate(self):
        self.proxy()

        self.assertEqual(self.certificates(), ["wildcard-a", "wildcard-b"])


def estate(size):
    """``size`` machines on the tailnet, each serving one name in one domain."""

    connected("example-tailnet", PROVIDERS[TAILNET_KIND].connection_providers[0])
    connected("example-cloudflare", "cloudflare_api")
    ManagedResource.objects.create(
        key="example-com",
        kind="cloudflare.zone",
        spec={"zone": "example.com", "connection_ref": "example-cloudflare"},
    )
    store("cloudflare.zone", {"zone": "example.com", "connection_ref": "example-cloudflare", "account_id": ACCOUNT})
    devices, records, apps = [], [], []
    for index in range(size):
        name, address = f"example-host-{index}", f"100.64.0.{index + 1}"
        ManagedResource.objects.create(key=name, kind="machine", spec={"name": name, "addresses": [address]})
        ManagedResource.objects.create(
            key=f"s{index}-dns",
            kind="adguard.rewrite",
            spec={"domain": f"s{index}.example.com", "answer": address},
        )
        devices.append({"name": name, "addresses": [address], "dns_name": f"{name}.example.ts.net", "online": True})
        records.append(dns(f"s{index}.example.com", address, proxied=True))
        apps.append(
            {
                "connection_ref": "example-cloudflare",
                "account_id": ACCOUNT,
                "id": f"a{index}",
                "name": f"example-gate-{index}",
                "domain": f"s{index}.example.com",
            }
        )
    store(TAILNET_KIND, *devices)
    store("cloudflare.dns_record", *records)
    store("cloudflare.access_app", *apps)
    store("cloudflare.edge_certificate", EDGE)
    store(
        "cloudflare.pages_project",
        {
            "connection_ref": "example-cloudflare",
            "account_id": ACCOUNT,
            "name": "example-site",
            "domains": ["example.com"],
        },
    )


class PageCostTests(TestCase):
    """The machine, service and domain pages do not cost more as the estate grows."""

    def arrange(self, size):
        ManagedResource.objects.all().delete()
        ProviderConnection.objects.all().delete()
        ProviderInventory.objects.all().delete()
        estate(size)

    def page(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response

    def test_the_machine_and_domain_pages_do_not_grow_with_the_estate(self):
        login(self.client)
        # Each page names every machine or every name the estate holds, so
        # the larger count is of the larger estate.
        for url, named in (
            (reverse("control_plane:machine", args=["example-host-0"]), r"example-host-\d+"),
            (reverse("zones:detail", args=["example.com"]), r"\bs\d+\.example\.com"),
        ):
            with self.subTest(url):
                assert_cost_does_not_grow(
                    self,
                    sizes=(2, 12),
                    arrange=self.arrange,
                    draw=lambda _size, url=url: self.page(url),
                    rows=distinct(named),
                )

    def test_the_service_page_does_not_grow_with_the_estate(self):
        # A service's page names only its own machine and name.
        login(self.client)
        url = reverse("control_plane:service", args=["s0.example.com"])

        assert_subject_cost_does_not_grow(self, sizes=(2, 12), arrange=self.arrange, draw=lambda _size: self.page(url))

    def test_the_relation_graph_is_read_once_per_page(self):
        estate(3)
        with projection_scope():
            relationships_for("machine:example-host-0", principal=READER)
            with CaptureQueriesContext(connection) as captured:
                relationships_for("service:s0.example.com", principal=READER)
                relationships_for("zone:example.com", principal=READER)
        self.assertEqual(len(captured), 0)


class _Mentions(HTMLParser):
    """Each text run in the page body, and whether a link or entity mark holds it."""

    SKIPPED = {"h1", "title", "pre", "script", "style", "option", "select", "textarea"}
    VOID = {"br", "img", "input", "meta", "link", "hr", "col", "source", "wbr"}

    def __init__(self):
        super().__init__()
        self.stack: list[tuple[str, bool, bool]] = []
        self.runs: list[tuple[str, bool]] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        attributes = dict(attrs)
        linked = tag == "a" or "data-entity" in attributes
        skipped = tag in self.SKIPPED or "page-trail" in (attributes.get("class") or "")
        self.stack.append((tag, linked, skipped))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        if not data.strip() or any(skipped for _tag, _linked, skipped in self.stack):
            return
        self.runs.append((data, any(linked for _tag, linked, _skipped in self.stack)))


class NoUnlinkedNamesTests(TestCase):
    """Every entity name on the five pages is a link builder mention."""

    NAMES = (
        "example-host-0",
        "s0.example.com",
        "example-site",
        "example-gate-0",
        "example-cloudflare",
        "example-tailnet",
    )

    def setUp(self):
        estate(2)
        login(self.client)

    def unlinked(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        main = body[body.index("<main") : body.index("</main>")]
        parser = _Mentions()
        parser.feed(main)
        found = []
        for run, linked in parser.runs:
            found.extend(
                (name, run.strip()[:80])
                for name in self.NAMES
                if re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w.-])", run) and not linked
            )
        return found

    def test_no_end_is_the_page_itself(self):
        # The zone's own declaration opens the domain page it is listed on.
        zone_page = entity_link("zone", "example.com").url
        found = relationships_for("zone:example.com", principal=READER)

        self.assertTrue(found.groups)
        self.assertNotIn(
            zone_page,
            [item.entity.url for group in found.groups for item in group.items],
        )

    def test_every_name_on_each_page_is_linked(self):
        for url in (
            reverse("control_plane:machine", args=["example-host-0"]),
            reverse("control_plane:service", args=["s0.example.com"]),
            reverse("zones:detail", args=["example.com"]),
            reverse("control_plane:connections"),
            reverse("control_plane:topology"),
        ):
            with self.subTest(url):
                self.assertEqual(self.unlinked(url), [])

    def test_the_pages_name_things_they_relate(self):
        machine = self.client.get(reverse("control_plane:machine", args=["example-host-0"]))
        service = self.client.get(reverse("control_plane:service", args=["s0.example.com"]))
        domain = self.client.get(reverse("zones:detail", args=["example.com"]))

        self.assertContains(machine, entity_link("connection", "example-tailnet").url)
        self.assertIn("example-gate-0", text(service))
        self.assertContains(
            domain,
            f'href="https://dash.cloudflare.com/{ACCOUNT}/pages/view/example-site"',
        )


class NoRawKindsTests(TestCase):
    def test_the_machine_page_names_its_declarations_by_label(self):
        estate(1)
        ManagedResource.objects.create(
            key="example-host-0-tailnet",
            kind=TAILNET_KIND,
            spec={"name": "example-host-0", "key_expiry_disabled": False},
        )
        login(self.client)

        page = self.client.get(reverse("control_plane:machine", args=["example-host-0"]))
        declaration = self.client.get(reverse("control_plane:detail", args=["example-host-0-tailnet"]))

        for response in (page, declaration):
            shown = re.sub(r"<pre>.*?</pre>", "", response.content.decode(), flags=re.DOTALL)
            self.assertNotIn(TAILNET_KIND, shown)
            self.assertNotIn("cloudflare.dns_record", shown)
        self.assertContains(declaration, kind_label(TAILNET_KIND))
        # The declaration page leads back to the machine it belongs to.
        self.assertContains(declaration, f'href="{reverse("control_plane:machine", args=["example-host-0"])}"')


@override_settings(SEVERINO_SITE_HOST="", ALLOWED_HOSTS=["testserver"])
class PolicyTestDefaultTests(TestCase):
    def test_the_policy_test_does_not_ask_a_machine_about_itself(self):
        estate(1)
        login(self.client)

        response = self.client.get(reverse("control_plane:machine", args=["example-host-0"]))

        self.assertEqual(response.context["asked"]["source"], "example-host-0")
        self.assertEqual(response.context["asked"]["target"], "")


class DomainCardTests(TestCase):
    """Four cards; parked names, a weaker posture and missing mail policy said plainly."""

    def setUp(self):
        from .test_zones import APEX, record, sweep

        sweep(
            zones=[
                {
                    "zone": "example.com",
                    "connection_ref": "cf-example",
                    "posture": {"ssl": "strict", "min_tls_version": "1.3"},
                },
                {
                    "zone": "example.net",
                    "connection_ref": "cf-example",
                    "posture": {"ssl": "full", "min_tls_version": "1.0"},
                },
            ],
            records=[
                *APEX,
                record("example.net", "A", "192.0.2.1", zone="example.net", rid="n1", proxied=True),
                record("www.example.net", "CNAME", "example.net", zone="example.net", rid="n2", proxied=True),
            ],
        )
        for key, name, rtype, content in (
            ("net-apex", "example.net", "A", "192.0.2.1"),
            ("net-www", "www.example.net", "CNAME", "example.net"),
        ):
            ManagedResource.objects.create(
                key=key,
                kind="cloudflare.dns_record",
                spec={
                    "zone": "example.net",
                    "name": name,
                    "record_type": rtype,
                    "content": content,
                    "proxied": True,
                    "ttl": 1,
                    "connection_ref": "cf-example",
                },
            )

    def cards(self, zone):
        from ..zones import find_zone

        return {card.label: card for card in find_zone(zone).cards}

    def test_a_parked_domain_says_so_with_who_handles_it(self):
        card = self.cards("example.net")["Services"]

        self.assertEqual(card.value, "Parked")
        self.assertIn("Cloudflare", card.detail)
        self.assertFalse(card.concern)

    def test_a_weaker_posture_than_another_domain_is_a_concern(self):
        weaker = self.cards("example.net")["Security"]
        stronger = self.cards("example.com")["Security"]

        self.assertTrue(weaker.value.startswith("Full"))
        self.assertIn("example.com", weaker.detail)
        self.assertTrue(weaker.concern)
        self.assertFalse(stronger.concern)

    def test_four_cards_and_missing_mail_policy_names_the_next_step(self):
        self.assertEqual(list(self.cards("example.net")), ["Services", "Security", "Email", "Registration"])
        card = self.cards("example.net")["Email"]

        self.assertEqual(card.value, "Not configured")
        self.assertIn("v=spf1 -all", card.note)
        self.assertEqual(card.url, reverse("zones:mail", args=["example.net"]))

    def test_the_record_count_is_the_domains_own(self):
        login(self.client)

        response = self.client.get(reverse("zones:detail", args=["example.net"]))

        # The domain's own count, not the sweep's total.
        self.assertContains(response, "2 records")


class MachineServesAgreesTests(TestCase):
    """A machine page's Serves header and its Relationships panel are one query."""

    def test_the_header_lists_every_name_the_panel_serves(self):
        from types import SimpleNamespace

        from ..machine_context import serves_links
        from ..relationships import RelationGroup, Relationship, Relationships

        panel = Relationships(
            node_id="machine:example-edge",
            groups=(
                RelationGroup(
                    RELATIONS["runs_on"].inverse,
                    10,
                    tuple(
                        Relationship(entity_link("service", name))
                        for name in ("a.example.com", "b.example.net", "c.example.org")
                    ),
                ),
            ),
        )
        machine = SimpleNamespace(hostnames=("a.example.com",), runs_hq=False, roles=())

        self.assertEqual(
            [link.label for link in serves_links(machine, panel)],
            list(panel.labels(RELATIONS["runs_on"].inverse)),
        )

    @SITE
    def test_the_page_header_and_panel_agree(self):
        declare("example-host", "192.0.2.44")
        login(self.client)

        with own("192.0.2.44"):
            response = self.client.get(reverse("control_plane:machine", args=["example-host"]))
            with projection_scope():
                panel = relationships_for("machine:example-host", principal=READER)

        served = panel.labels(RELATIONS["runs_on"].inverse)
        self.assertTrue(served)
        for name in served:
            self.assertContains(response, entity_link("service", name).url)


class UnreadableScopeTests(TestCase):
    def test_a_refused_hostname_reading_is_named_on_a_service_and_not_elsewhere(self):
        kind = next(kind for kind, spec in OBSERVATIONS.items() if spec.joins_hostnames and not spec.joins_addresses)
        store(kind, reachable=False, error="refused")
        connected("example-ssh", "ssh")
        declare("example-host", "192.0.2.44")

        with projection_scope():
            graph = relation_graph(principal=READER)
            nodes = {node.kind: node.id for node in graph.topology.nodes}
            label = OBSERVATIONS[kind].label
            machine = relationships_for("machine:example-host", principal=READER)
            connection = relationships_for(nodes["connection"], principal=READER)

        self.assertNotIn(label, connection.unreadable)
        self.assertFalse(graph.subjects["machine:example-host"].dns_names)
        self.assertNotIn(label, machine.unreadable)
        self.assertIn(label, unreadable_labels())


class RelationListTests(TestCase):
    """Each relation is named once; who read its ends is said once where one source read them all."""

    def render(self, *items):
        from django.template.loader import render_to_string

        from ..relationships import RelationGroup, Relationships

        panel = Relationships(node_id="machine:example", groups=(RelationGroup("Carries", 10, items),))
        return render_to_string("partials/_relationships.html", {"relationships": panel})

    def test_a_relation_is_named_once_with_every_end(self):
        from ..relationships import Relationship

        html = self.render(*(Relationship(EntityLink(name)) for name in ("alpha", "beta", "gamma")))

        self.assertEqual(html.count("Carries"), 1)
        for name in ("alpha", "beta", "gamma"):
            self.assertEqual(html.count(name), 1)

    def test_one_source_for_every_end_is_said_once(self):
        from django.utils import timezone

        from ..relationships import Relationship

        source = EntityLink("example-reader")
        html = self.render(
            Relationship(EntityLink("alpha"), source, timezone.now()),
            Relationship(EntityLink("beta"), source, timezone.now()),
        )

        self.assertEqual(html.count("example-reader"), 1)

    def test_ends_read_by_different_sources_each_say_theirs(self):
        from django.utils import timezone

        from ..relationships import Relationship

        html = self.render(
            Relationship(EntityLink("alpha"), EntityLink("example-reader"), timezone.now()),
            Relationship(EntityLink("beta"), EntityLink("example-other"), timezone.now()),
        )

        self.assertEqual(html.count("example-reader"), 1)
        self.assertEqual(html.count("example-other"), 1)
