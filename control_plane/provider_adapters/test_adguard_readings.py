"""AdGuard readings: clients, the query-log aggregate and its privacy, and DNS posture."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from types import MappingProxyType, SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, TestCase

from application.inventory import record_inventory
from application.security import cli_principal
from control_plane.models import ProviderInventory
from control_plane.observations import OBSERVATIONS
from control_plane.observations.adguard import (
    CLIENT_KIND,
    CLIENTS_KEPT,
    DNS_KIND,
    FILTERING_OFF,
    NAME_UNUSED,
    PLAIN_UPSTREAM,
    PROTECTION_OFF,
    QUERY_KIND,
    plain_upstreams,
    upstream,
)
from control_plane.providers import CONTROLLER_PROVIDER_ADAPTERS
from control_plane.reading_parts import clean_refused_parts, refused_parts

from . import adguard, adguard_readings
from .contracts import (
    ControllerIntegrationAdapter,
    ProviderError,
    compile_controller_adapters,
)
from .parts import part_ledger

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
# Names the fixtures query that no rewrite answers for. They must never be kept.
PRIVATE_NAMES = ("private-browsing.example.org", "bank.example.net")


class FakeAdGuard:
    """Each connection's AdGuard as path -> answer; records every path asked."""

    def __init__(self, answers: dict[str, dict[str, object]]):
        self.answers = answers
        self.asked: list[tuple[str, str]] = []

    def connection_refs(self, provider):
        return tuple(self.answers)

    def snapshot_value(self, key, load):
        return load()

    def connection_prefix(self, provider, connection_ref=""):
        return connection_ref.upper() or provider.upper()

    def required(self, prefix, name):
        return f"https://{prefix.lower()}.example.test" if name == "URL" else "x"

    def request(self, url, *, method="GET", headers=None, payload=None):
        host, _, path = url.removeprefix("https://").partition("/")
        ref = host.split(".", 1)[0]
        path = f"/{path}"
        self.asked.append((ref, path))
        answers = self.answers[ref]
        key = path.split("?", 1)[0]
        if key == "/control/querylog":
            return answers["querylog"](path)
        answer = answers[key]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def query(name, client, when, *, reason="NotFilteredNotFound", client_name=""):
    return {
        "question": {"name": name, "type": "A", "class": "IN"},
        "client": client,
        "client_info": {"name": client_name, "whois": {"country": "XX"}},
        "reason": reason,
        "time": when.isoformat(),
        "answer": [{"type": "A", "value": "192.0.2.99", "ttl": 60}],
        "upstream": "tls://dns.example.test",
        "elapsedMs": "1.2",
    }


def log(entries, *, page=adguard_readings.QUERY_PAGE):
    """A querylog endpoint paging ``entries`` (newest first) by ``older_than``."""

    def answer(path):
        start = 0
        if "older_than=" in path:
            marker = path.split("older_than=", 1)[1].split("&", 1)[0]
            from urllib.parse import unquote

            marker = unquote(marker)
            start = next(
                index + 1 for index, item in enumerate(entries) if item["time"] == marker
            )
        chunk = entries[start : start + page]
        return {"data": chunk, "oldest": chunk[-1]["time"] if chunk else ""}

    return answer


def surface(entries=(), *, rewrites=None, config=None, **overrides):
    found = {
        "/control/rewrite/list": rewrites
        if rewrites is not None
        else [
            {"domain": "app.example.com", "answer": "100.64.0.10", "enabled": True},
            {"domain": "idle.example.com", "answer": "100.64.0.10", "enabled": True},
            {"domain": "*.lab.example.com", "answer": "100.64.0.11", "enabled": True},
            {"domain": "off.example.com", "answer": "100.64.0.10", "enabled": False},
        ],
        "/control/querylog/config": config
        if config is not None
        else {"enabled": True, "interval": 86_400_000, "anonymize_client_ip": False},
        "querylog": log(list(entries)),
    }
    found.update(overrides)
    return found


def read(kind, runtime):
    return adguard.READINGS[kind](runtime)


def read_with_parts(kind, runtime):
    """The records and the parts refused, as the stored snapshot says them."""

    with part_ledger() as ledger:
        records = read(kind, runtime)
    snapshot = SimpleNamespace(
        kind=kind, reachable=True, refused_parts=clean_refused_parts(kind, ledger)
    )
    return records, refused_parts(snapshot)


class QuerySummaryTests(SimpleTestCase):
    def summarize(self, entries, **kwargs):
        records, _refused, runtime = self.summarize_with_parts(entries, **kwargs)
        return records, runtime

    def summarize_with_parts(self, entries, **kwargs):
        runtime = FakeAdGuard({"home": surface(entries, **kwargs)})
        with mock.patch.object(adguard_readings, "datetime", wraps=datetime) as clock:
            clock.now.return_value = NOW
            records, refused = read_with_parts(QUERY_KIND, runtime)
        return records, refused, runtime

    def test_each_rewritten_name_is_counted_and_nothing_else_is_named(self):
        entries = [
            query("app.example.com", "100.64.0.2", NOW - timedelta(minutes=1), client_name="laptop"),
            query("app.example.com", "100.64.0.3", NOW - timedelta(minutes=2)),
            query("app.example.com", "100.64.0.2", NOW - timedelta(minutes=3)),
            query("grafana.lab.example.com", "100.64.0.2", NOW - timedelta(minutes=4),
                  reason="FilteredBlackList"),
            *(query(name, "100.64.0.2", NOW - timedelta(minutes=5)) for name in PRIVATE_NAMES),
        ]

        records, _ = self.summarize(entries)
        by_name = {record["domain"]: record for record in records}

        self.assertEqual(sorted(by_name), ["app.example.com", "grafana.lab.example.com",
                                           "idle.example.com"])
        app = by_name["app.example.com"]
        self.assertEqual((app["queries"], app["client_count"], app["blocked"]), (3, 2, 0))
        self.assertEqual(app["clients"][0], {"address": "100.64.0.2", "name": "laptop",
                                             "queries": 2})
        self.assertEqual(app["last_seen"], (NOW - timedelta(minutes=1)).isoformat())
        self.assertEqual(app["connection_ref"], "home")
        self.assertEqual(by_name["grafana.lab.example.com"]["blocked"], 1)
        self.assertEqual(by_name["idle.example.com"]["queries"], 0)

    def test_the_stored_aggregate_carries_no_query_and_no_other_name(self):
        """The privacy class: whatever the log holds, only the aggregate is kept."""

        entries = [
            query("app.example.com", "100.64.0.2", NOW - timedelta(minutes=1)),
            *(query(name, "100.64.0.9", NOW - timedelta(minutes=2)) for name in PRIVATE_NAMES),
        ]
        records, _ = self.summarize(entries)
        kept, refused = OBSERVATIONS[QUERY_KIND].clean(records)
        dumped = json.dumps(kept)

        self.assertEqual(refused, 0)
        for name in PRIVATE_NAMES:
            self.assertNotIn(name, dumped)
        for raw in ("question", "192.0.2.99", "whois", "upstream", "elapsedMs", "100.64.0.9"):
            self.assertNotIn(raw, dumped)
        self.assertLessEqual(
            set().union(*(record.keys() for record in kept)),
            set(OBSERVATIONS[QUERY_KIND].record.model_fields),
        )

    def test_a_disabled_rewrite_is_not_a_name_to_count(self):
        records, _ = self.summarize([query("off.example.com", "100.64.0.2", NOW)])

        self.assertNotIn("off.example.com", {record["domain"] for record in records})

    def test_the_clients_kept_are_capped_and_the_count_is_not(self):
        entries = [
            query("app.example.com", f"100.64.0.{index}", NOW - timedelta(seconds=index))
            for index in range(1, CLIENTS_KEPT + 6)
        ]
        records, _ = self.summarize(entries)
        app = next(record for record in records if record["domain"] == "app.example.com")

        self.assertEqual(app["client_count"], CLIENTS_KEPT + 5)
        self.assertEqual(len(app["clients"]), CLIENTS_KEPT)

    def test_anonymized_addresses_are_not_kept_as_clients(self):
        records, (refused,), _ = self.summarize_with_parts(
            [query("app.example.com", "100.64.0.0", NOW)],
            config={"enabled": True, "interval": 86_400_000, "anonymize_client_ip": True},
        )
        app = next(record for record in records if record["domain"] == "app.example.com")

        self.assertNotIn("clients", app)
        self.assertNotIn("unread", app)
        self.assertEqual(app["queries"], 1)
        self.assertEqual((refused.part.name, refused.connection_ref), ("clients", "home"))
        self.assertEqual(
            refused.phrase, "Clients looking it up not read: AdGuard anonymizes client addresses"
        )

    def test_client_addresses_kept_refuse_no_part(self):
        _records, refused, _ = self.summarize_with_parts(
            [query("app.example.com", "100.64.0.2", NOW)]
        )

        self.assertEqual(refused, ())

    def test_the_log_is_paged_back_to_the_window_and_no_further(self):
        entries = [
            query("app.example.com", "100.64.0.2", NOW - timedelta(minutes=10 * index))
            for index in range(1, 200)
        ]
        with mock.patch.object(adguard_readings, "QUERY_PAGE", 50):
            runtime = FakeAdGuard({"home": surface(
                entries, querylog=log(entries, page=50))})
            with mock.patch.object(adguard_readings, "datetime", wraps=datetime) as clock:
                clock.now.return_value = NOW
                records = read(QUERY_KIND, runtime)
        pages = [path for _ref, path in runtime.asked if path.startswith("/control/querylog?")]
        app = next(record for record in records if record["domain"] == "app.example.com")

        # 24 hours at one query every 10 minutes, both ends inclusive.
        self.assertEqual(app["queries"], 144)
        self.assertEqual(app["window_hours"], 24.0)
        self.assertEqual(len(pages), 3)
        self.assertIn("older_than=", pages[1])

    def test_the_page_limit_bounds_the_read_and_the_window_says_so(self):
        entries = [
            query("app.example.com", "100.64.0.2", NOW - timedelta(minutes=index))
            for index in range(1, 400)
        ]
        with (
            mock.patch.object(adguard_readings, "QUERY_PAGE", 50),
            mock.patch.object(adguard_readings, "QUERY_PAGES", 2),
        ):
            runtime = FakeAdGuard({"home": surface(entries, querylog=log(entries, page=50))})
            with mock.patch.object(adguard_readings, "datetime", wraps=datetime) as clock:
                clock.now.return_value = NOW
                records = read(QUERY_KIND, runtime)
        app = next(record for record in records if record["domain"] == "app.example.com")

        self.assertEqual(app["queries"], 100)
        self.assertEqual(app["window_hours"], 1.7)

    def test_a_query_log_that_is_off_is_a_refused_read(self):
        runtime = FakeAdGuard({"home": surface(config={"enabled": False})})

        with self.assertRaisesRegex(ProviderError, "query log is off"):
            read(QUERY_KIND, runtime)

    def test_each_connection_is_read_and_attributed_on_its_own(self):
        runtime = FakeAdGuard({
            "home": surface([query("app.example.com", "100.64.0.2", NOW)]),
            "cabin": surface([], rewrites=[{"domain": "cabin.example.com", "answer": "x"}]),
        })
        with mock.patch.object(adguard_readings, "datetime", wraps=datetime) as clock:
            clock.now.return_value = NOW
            records = read(QUERY_KIND, runtime)

        self.assertEqual(
            {(record["connection_ref"], record["domain"]) for record in records},
            {("home", "app.example.com"), ("home", "idle.example.com"),
             ("cabin", "cabin.example.com")},
        )

    def test_a_name_nobody_looked_up_is_a_fact_only_over_a_long_enough_window(self):
        facts = OBSERVATIONS[QUERY_KIND].facts
        record = {"domain": "idle.example.com", "queries": 0, "window_hours": 24.0}

        self.assertEqual(facts(record), ((NAME_UNUSED, "idle.example.com"),))
        self.assertEqual(facts({**record, "window_hours": 2.0}), ())
        self.assertEqual(facts({**record, "queries": 1}), ())

    def test_titles_say_the_counts_and_who(self):
        title = OBSERVATIONS[QUERY_KIND].title

        self.assertEqual(
            title({"queries": 1, "client_count": 1, "window_hours": 24,
                   "clients": [{"address": "100.64.0.2", "name": "laptop"}]}),
            "1 lookup from 1 device in 24 hours: laptop",
        )
        self.assertEqual(
            title({"queries": 9, "client_count": 5, "blocked": 2, "window_hours": 72,
                   "clients": [{"address": f"100.64.0.{i}"} for i in range(5)]}),
            "9 lookups from 5 devices in 3 days, 2 blocked: "
            "100.64.0.0, 100.64.0.1, 100.64.0.2 and 2 more",
        )
        self.assertEqual(
            title({"queries": 0, "window_hours": 24}), "Not looked up in the last 24 hours"
        )


class ClientTests(SimpleTestCase):
    def test_persistent_and_runtime_clients_join_by_address_only(self):
        runtime = FakeAdGuard({"home": {"/control/clients": {
            "clients": [{"name": "laptop", "ids": ["100.64.0.2", "aa:bb:cc:dd:ee:ff",
                                                   "192.0.2.0/24", "laptop-doh"],
                         "use_global_settings": True, "filtering_enabled": True}],
            "auto_clients": [
                {"ip": "100.64.0.2", "name": "shadowed", "source": "ARP"},
                {"ip": "100.64.0.3", "name": "phone.example.test", "source": "rDNS",
                 "whois_info": {"orgname": "Example ISP"}},
                {"ip": "not-an-address", "name": "junk", "source": "etc/hosts"},
            ],
        }}})

        records = read(CLIENT_KIND, runtime)
        kept, _ = OBSERVATIONS[CLIENT_KIND].clean(records)

        self.assertEqual(
            [(r["name"], r["source"], r["addresses"]) for r in kept],
            [("laptop", "persistent", ["100.64.0.2"]), ("phone.example.test", "rdns", ["100.64.0.3"])],
        )
        self.assertEqual(kept[0]["ids"][1], "aa:bb:cc:dd:ee:ff")
        self.assertNotIn("Example ISP", json.dumps(kept))
        self.assertEqual({r["connection_ref"] for r in kept}, {"home"})
        self.assertEqual(OBSERVATIONS[CLIENT_KIND].addresses(kept[1]), ("100.64.0.3",))

    def test_a_refused_client_read_raises(self):
        runtime = FakeAdGuard({"home": {"/control/clients": ProviderError("refused")}})

        with self.assertRaises(ProviderError):
            read(CLIENT_KIND, runtime)


class UpstreamTests(SimpleTestCase):
    def test_upstream_lines_as_adguard_writes_them(self):
        cases = {
            "192.0.2.53": ("192.0.2.53", "dns", ()),
            "udp://192.0.2.53:53": ("192.0.2.53", "dns", ()),
            "tcp://[2001:db8::1]:53": ("2001:db8::1", "dns", ()),
            "tls://dns.example.test": ("dns.example.test", "tls", ()),
            "https://dns.example.test/dns-query/account-id": ("dns.example.test", "https", ()),
            "quic://dns.example.test": ("dns.example.test", "quic", ()),
            "sdns://AgcAAAAAAAAAAA": ("", "dnscrypt", ()),
            "[/lan/home.arpa/]10.0.0.53": ("10.0.0.53", "dns", ("lan", "home.arpa")),
        }
        for line, (host, transport, domains) in cases.items():
            with self.subTest(line):
                self.assertEqual(
                    upstream(line), {"host": host, "transport": transport, "domains": domains}
                )
        for line in ("", "# a comment", "[/lan/]#", "ftp://192.0.2.1"):
            with self.subTest(line):
                self.assertIsNone(upstream(line))

    def test_a_path_that_can_carry_an_account_never_leaves(self):
        self.assertNotIn("account-id", json.dumps(upstream("https://dns.example.test/q/account-id")))

    def test_only_plain_upstreams_off_the_site_are_plain(self):
        record = {"upstreams": [
            {"host": "198.51.100.53", "transport": "dns"},
            {"host": "10.0.0.53", "transport": "dns"},
            {"host": "100.64.0.53", "transport": "dns"},
            {"host": "dns.example.test", "transport": "tls"},
            {"host": "", "transport": "dnscrypt"},
        ]}

        self.assertEqual(plain_upstreams(record), ("198.51.100.53",))


class DnsPostureTests(SimpleTestCase):
    def posture(self, **overrides):
        answers = {
            "/control/status": {"version": "v0.107.99", "running": True,
                                "protection_enabled": True,
                                "dns_addresses": ["0.0.0.0", "100.64.0.53"]},
            "/control/dns_info": {"upstream_dns": ["tls://dns.example.test", "198.51.100.53",
                                                   "# fallback"],
                                  "upstream_mode": "parallel", "dnssec_enabled": True},
            "/control/filtering/status": {"enabled": True, "filters": [
                {"enabled": True, "rules_count": 1000}, {"enabled": False, "rules_count": 9}]},
            "/control/querylog/config": {"enabled": True, "interval": 7_776_000_000,
                                         "anonymize_client_ip": False, "ignored": []},
            "/control/rewrite/settings": {"enabled": True},
        }
        answers.update(overrides)
        (record,), self.refused = read_with_parts(DNS_KIND, FakeAdGuard({"home": answers}))
        kept, refused = OBSERVATIONS[DNS_KIND].clean([record])
        self.assertEqual(refused, 0)
        return kept[0]

    def test_the_posture_is_read_whole(self):
        record = self.posture()

        self.assertEqual(record["version"], "v0.107.99")
        self.assertEqual((record["filter_lists"], record["filter_rules"]), (1, 1000))
        self.assertEqual(record["querylog_retention_hours"], 2160.0)
        self.assertEqual([item["transport"] for item in record["upstreams"]], ["tls", "dns"])
        self.assertEqual(OBSERVATIONS[DNS_KIND].addresses(record), ("100.64.0.53",))
        self.assertEqual(
            OBSERVATIONS[DNS_KIND].facts(record), ((PLAIN_UPSTREAM, "198.51.100.53"),)
        )
        self.assertEqual(self.refused, ())

    def test_off_switches_are_facts(self):
        record = self.posture(**{
            "/control/status": {"version": "v1", "protection_enabled": False},
            "/control/filtering/status": {"enabled": False, "filters": []},
        })

        facts = OBSERVATIONS[DNS_KIND].facts(record)
        self.assertIn((PROTECTION_OFF, "Off"), facts)
        self.assertIn((FILTERING_OFF, "Off"), facts)
        self.assertIn("protection off", OBSERVATIONS[DNS_KIND].title(record))

    def test_a_part_an_older_adguard_lacks_is_a_refused_part_and_the_rest_kept(self):
        record = self.posture(**{"/control/rewrite/settings": ProviderError("HTTPError.")})

        (refused,) = self.refused
        self.assertEqual((refused.part.name, refused.connection_ref), ("rewrites", "home"))
        self.assertEqual(refused.phrase, "Rewrite settings not read: HTTPError")
        self.assertNotIn("unread", record)
        self.assertTrue(record["filtering_enabled"])
        self.assertNotIn("rewrites_enabled", record)

    def test_an_answer_that_does_not_parse_is_that_part_refused(self):
        self.posture(**{"/control/filtering/status": {"enabled": True, "filters": [None]}})

        self.assertEqual([refused.part.name for refused in self.refused], ["filtering"])

    def test_a_server_that_gives_no_status_is_a_refused_read(self):
        runtime = FakeAdGuard({"home": {"/control/status": ProviderError("refused")}})

        with self.assertRaises(ProviderError):
            read(DNS_KIND, runtime)


class RewriteAttributionTests(SimpleTestCase):
    def test_each_rewrite_names_the_connection_that_holds_it(self):
        runtime = FakeAdGuard({
            "home": {"/control/rewrite/list": [{"domain": "a.example.com", "answer": "x"}]},
            "cabin": {"/control/rewrite/list": [{"domain": "b.example.com", "answer": "y"}]},
        })

        self.assertEqual(
            adguard.inventory(runtime),
            [
                {"connection_ref": "home", "domain": "a.example.com", "answer": "x",
                 "enabled": True},
                {"connection_ref": "cabin", "domain": "b.example.com", "answer": "y",
                 "enabled": True},
            ],
        )


class ReadingContractTests(SimpleTestCase):
    def adapter(self):
        return next(
            item for item in CONTROLLER_PROVIDER_ADAPTERS
            if any(d.kind == "adguard.rewrite" for d in item.definitions)
        )

    def test_the_adguard_adapter_reads_its_three_readings(self):
        registry = compile_controller_adapters((self.adapter(),), mock.Mock())

        self.assertEqual(set(registry.readings), {CLIENT_KIND, QUERY_KIND, DNS_KIND})
        for kind in registry.readings:
            self.assertEqual(OBSERVATIONS[kind].provider, "adguard")
            self.assertEqual(OBSERVATIONS[kind].requires, ())

    def test_an_integration_with_no_resource_kind_and_no_reading_is_refused(self):
        with self.assertRaisesRegex(ValueError, "resource kind or a reading"):
            ControllerIntegrationAdapter(
                definitions=(), inventory={}, connection_probes={}, actions={}
            )

    def test_a_reading_only_integration_reads_through_the_connection_it_names(self):
        adapter = ControllerIntegrationAdapter(
            definitions=(), inventory={}, connection_probes={}, actions={},
            readings={CLIENT_KIND: lambda runtime: []}, reads_through=("adguard",),
        )

        self.assertEqual(set(adapter.readings), {CLIENT_KIND})
        with self.assertRaisesRegex(ValueError, "does not hold"):
            ControllerIntegrationAdapter(
                definitions=(), inventory={}, connection_probes={}, actions={},
                readings={CLIENT_KIND: lambda runtime: []}, reads_through=("npm",),
            )

    def test_an_unregistered_reading_is_refused(self):
        with self.assertRaisesRegex(ValueError, "unregistered reading"):
            replace(self.adapter(), readings={"example.nothing": lambda runtime: []})

    def test_a_reading_through_a_connection_the_integration_lacks_is_refused(self):
        with self.assertRaisesRegex(ValueError, "does not hold"):
            replace(self.adapter(), readings={"tailscale.dns": lambda runtime: []})

    def test_two_readers_for_one_reading_are_refused(self):
        @dataclass(frozen=True)
        class Watcher:
            kind: str = "example.watcher"
            actions = MappingProxyType({})
            connection_providers: tuple[str, ...] = ("adguard",)
            unobserved_reason: str = "Only reads."

        second = ControllerIntegrationAdapter(
            definitions=(Watcher(),),
            inventory={},
            connection_probes={"adguard": lambda runtime, ref: {}},
            actions={},
            readings={CLIENT_KIND: lambda runtime: []},
        )

        with self.assertRaisesRegex(ValueError, "Duplicate reader"):
            compile_controller_adapters((self.adapter(), second), mock.Mock())


class StoredReadingTests(TestCase):
    def test_a_stored_summary_keeps_only_its_schema(self):
        record = {
            "connection_ref": "home", "domain": "app.example.com", "queries": 1,
            "client_count": 1, "window_hours": 24.0,
            "clients": [{"address": "100.64.0.2", "name": "laptop", "queries": 1,
                         "question": "bank.example.net"}],
            "raw": [query("bank.example.net", "100.64.0.2", NOW)],
        }
        record_inventory({QUERY_KIND: {"ok": True, "records": [record]}},
                         principal=cli_principal())

        stored = ProviderInventory.objects.get(kind=QUERY_KIND).records
        self.assertNotIn("bank.example.net", json.dumps(stored))
        self.assertEqual(stored[0]["clients"][0]["name"], "laptop")
