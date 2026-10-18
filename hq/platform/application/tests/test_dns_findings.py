"""What the AdGuard readings become as findings, read off the facts on each connection."""

from types import SimpleNamespace

from django.test import SimpleTestCase

from hq.domains.control_plane.observations.adguard import NAME_UNUSED, PLAIN_UPSTREAM

from ..dns_findings import plain_upstream, unused_names


def connection(label, *facts):
    return SimpleNamespace(kind="connection", id=f"connection:{label}", label=label, facts=facts)


def estate(*nodes):
    return SimpleNamespace(nodes=lambda: nodes)


def upstreams(*hosts):
    return tuple((PLAIN_UPSTREAM, host) for host in hosts)


class PlainUpstreamTests(SimpleTestCase):
    def test_a_connection_with_nothing_unencrypted_does_not_end_the_search(self):
        found = plain_upstream(estate(connection("adguard-a"), connection("adguard-b", *upstreams("192.0.2.53"))))

        self.assertEqual([finding["subject"] for finding in found], ["connection:adguard-b"])
        self.assertEqual(found[0]["evidence"], (("Unencrypted server", "192.0.2.53"),))

    def test_two_servers_are_named_and_more_are_counted(self):
        named = ("192.0.2.1", "192.0.2.2")
        counted = ("192.0.2.1", "192.0.2.2", "192.0.2.3")

        (two,) = plain_upstream(estate(connection("adguard", *upstreams(*named))))
        (three,) = plain_upstream(estate(connection("adguard", *upstreams(*counted))))

        self.assertTrue(all(host in two["title"] for host in named))
        self.assertFalse(any(host in three["title"] for host in counted))
        self.assertIn("3", three["title"])
        self.assertEqual(len(three["evidence"]), 3)

    def test_findings_come_in_title_order(self):
        found = plain_upstream(
            estate(connection("zeta", *upstreams("192.0.2.9")), connection("alpha", *upstreams("192.0.2.8")))
        )

        titles = [finding["title"] for finding in found]
        self.assertEqual(titles, sorted(titles))
        self.assertEqual(len(titles), 2)


class UnusedNameTests(SimpleTestCase):
    def test_findings_come_in_title_order(self):
        reader = connection("adguard", (NAME_UNUSED, "zeta.example.com"), (NAME_UNUSED, "alpha.example.com"))

        titles = [finding["title"] for finding in unused_names(estate(reader))]

        self.assertEqual(titles, sorted(titles))
        self.assertEqual(len(titles), 2)

    def test_a_name_hq_serves_is_found_on_its_service(self):
        service = SimpleNamespace(kind="service", id="service:app", label="app.example.com", facts=())
        reader = connection("adguard", (NAME_UNUSED, "app.example.com"), (NAME_UNUSED, "old.example.com"))

        subjects = {finding["evidence"][0][1]: finding["subject"] for finding in unused_names(estate(service, reader))}

        self.assertEqual(subjects, {"app.example.com": "service:app", "old.example.com": "connection:adguard"})
