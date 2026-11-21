"""The connections page's security story is the standards' checks, measured once."""

from dataclasses import replace
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.platform.application.markup_testing import select
from hq.platform.core.tests.forwarding import arrived

from ..connection_catalog import ConnectionAbilityState, ConnectionGroup, ConnectionView
from ..connection_security import connection_security_posture
from ..connections import (
    ConnectionAbility,
    ConnectionInstance,
    ConnectionLink,
    ConnectionSpec,
)
from ..facts import stored_snapshots
from ..moments import ago
from ..posture import Inputs, posture
from ..projection import projection_scope
from ..request_connection import connection
from ..security import Capability
from ..standards import Reading, Standing


# The readings are imported where they are read, so this module imports at a
# base that names them otherwise (`mise run test --at`).
def observed_edge(hostname):
    from ..connection_security import request_readings

    return request_readings(hostname)[0]


def observed_firewall():
    from ..connection_security import request_readings

    return request_readings("hq.example.test")[1]


def _posture(groups, request=None):
    inputs = Inputs(edge=Reading.unavailable("Not read"), firewall=Reading.unavailable("Not read"), groups=groups)
    return connection_security_posture(
        inputs, posture(inputs), connection=connection(request) if request is not None else None
    )


def _result(found, check_id):
    return next(result for result in found.results if result.check.id == check_id)


def _groups(*, ability_available=True, status="good", required_scopes=("example:read",), observed=True):
    ability = ConnectionAbility(
        "example.read",
        "Read example",
        "Read one synthetic account.",
        required_scopes=required_scopes,
    )
    instance = ConnectionInstance(
        "example:one",
        "Example",
        "example",
        status,
        "healthy" if status == "good" else "attention",
        scopes_known=ability_available is not None,
        granted_scopes=("example:read",) if ability_available else (),
        ability_names=(ability.name,),
        dependencies=(ConnectionLink("example.resource"),),
        credential_model="scoped",
        observed_at=timezone.now() if observed else None,
    )
    spec = ConnectionSpec(
        "example.connections",
        "Example connections",
        "Synthetic connection contract.",
        Capability.READ,
        lambda: (instance,),
        (ability,),
        secret_store="Example Vault",
    )
    return (
        ConnectionGroup(
            spec,
            (
                ConnectionView(
                    instance,
                    lifecycle={"good": "ready", "serious": "unreachable"}.get(status, "configured"),
                    abilities=(
                        ConnectionAbilityState(
                            ability,
                            available=ability_available,
                            missing_scopes=() if ability_available is not False else ("example:read",),
                            action=None,
                            evidence=(
                                "unknown"
                                if ability_available is None
                                else "missing"
                                if ability_available is False
                                else "verified"
                                if required_scopes
                                else "undeclared"
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )


class OneStateModelTests(TestCase):
    """The headline counts the lifecycle each row shows, so they cannot disagree."""

    def _posture_with(self, lifecycle):
        (group,) = _groups()
        view = replace(group.connections[0], lifecycle=lifecycle)
        return _posture((replace(group, connections=(view,)),))

    def test_each_lifecycle_lands_in_exactly_one_headline_bucket(self):
        for lifecycle, bucket in (
            ("ready", "healthy_count"),
            ("reachable", "healthy_count"),
            ("stale", "attention_count"),
            ("unauthorized", "attention_count"),
            ("unreachable", "attention_count"),
            ("revoked", "attention_count"),
            ("configured", "unverified_count"),
        ):
            with self.subTest(lifecycle=lifecycle):
                posture = self._posture_with(lifecycle)
                counts = {
                    name: getattr(posture, name) for name in ("healthy_count", "attention_count", "unverified_count")
                }
                self.assertEqual(counts[bucket], 1, counts)
                self.assertEqual(sum(counts.values()), 1, counts)

    def test_the_posture_is_the_worst_its_rows_show(self):
        for lifecycle, state in {"configured": "neutral", "stale": "attention", "revoked": "serious"}.items():
            with self.subTest(lifecycle=lifecycle):
                self.assertEqual(self._posture_with(lifecycle).state, state)

    def test_a_permission_not_held_is_never_green_and_a_fault_under_advanced_posture(self):
        (group,) = _groups()
        view = group.connections[0]
        for evidence, state, advanced in (
            ("not_applicable", "good", ""),
            ("coarse", "neutral", ""),
            ("unverified", "neutral", ""),
            ("revoked", "neutral", ""),
            ("revoked", "serious", "advanced"),
        ):
            with self.subTest(evidence=evidence, advanced=advanced), override_settings(SEVERINO_POSTURE=advanced):
                judged = replace(view, abilities=(replace(view.abilities[0], evidence=evidence),))
                self.assertEqual(_posture((replace(group, connections=(judged,)),)).state, state)

    def test_without_a_request_the_posture_is_the_estate_alone(self):
        posture = _posture(_groups())

        self.assertEqual(posture.headline, "")
        self.assertEqual(posture.admission, ())
        self.assertEqual(posture.state, "good")

    def test_a_whole_account_credential_is_named_and_still_ready(self):
        from hq.platform.application.connection_catalog import (
            AUTHORITY_LABELS,
            connection_authority,
            connection_lifecycle,
        )

        (group,) = _groups()
        view = group.connections[0]
        coarse = (replace(view.abilities[0], evidence="coarse"),)
        authority = connection_authority(coarse)

        self.assertEqual(authority, "whole_account")
        self.assertEqual(AUTHORITY_LABELS[authority], "Credential has full account access")
        self.assertEqual(connection_lifecycle(view.instance, authority, now=timezone.now()), "ready")


@override_settings(SEVERINO_ENFORCE_TRUSTED_NETWORK=True, ALLOWED_HOSTS=["hq.example.test"])
class ConnectionSecurityPostureTests(TestCase):
    def request(self, address="100.64.0.5", *, secure=True, **extra):
        return RequestFactory().get(
            "/infrastructure/connections/",
            secure=secure,
            HTTP_HOST="hq.example.test",
            **arrived(REMOTE_ADDR=address, **extra),
        )

    def test_tailnet_ingress_and_connection_authority_are_derived_together(self):
        posture = _posture(_groups(), self.request())

        self.assertEqual(posture.headline, "You are connected over the tailnet.")
        self.assertEqual(posture.external_custody_count, 1)
        self.assertEqual(posture.dependency_count, 1)
        self.assertEqual(_result(posture, "scope").reading.evidence, "1 checked")

    def test_plain_http_over_the_tailnet_is_encrypted_by_wireguard(self):
        posture = _posture(_groups(), self.request(secure=False))

        self.assertTrue(_result(posture, "transport").met)
        self.assertEqual(posture.headline, "You are connected over the tailnet.")

    @override_settings(SEVERINO_TRUSTED_PROXIES=["10.0.0.9/32"])
    def test_the_current_trusted_proxy_path_is_proven_without_a_probe(self):
        posture = _posture(_groups(), self.request("10.0.0.9", HTTP_X_FORWARDED_FOR="100.64.0.5"))

        self.assertTrue(_result(posture, "forwarder").met)
        self.assertEqual(posture.trusted_proxy_count, 1)

    def test_a_request_not_forwarded_has_no_proxy_check(self):
        posture = _posture(_groups(), self.request())

        self.assertEqual({result.check.id for result in posture.admission}, {"gate", "transport"})

    def test_unknown_scope_evidence_is_not_read_rather_than_judged(self):
        posture = _posture(_groups(ability_available=None), self.request())

        scope = _result(posture, "scope")
        self.assertEqual(scope.state, Standing.UNAVAILABLE)
        self.assertIn("1 not reported", scope.reading.evidence)
        self.assertEqual(posture.state, "neutral")

    def test_an_ability_that_declared_no_proof_is_reported_as_unchecked(self):
        # An ability with no grant model and no credential model has told HQ
        # nothing, and the posture carries that as a visible debt.
        posture = _posture(_groups(ability_available=True, required_scopes=()), self.request())

        scope = _result(posture, "scope")
        self.assertEqual(scope.state, Standing.UNAVAILABLE)
        self.assertIn("1 not checked", scope.reading.evidence)
        self.assertNotIn("not reported", scope.reading.evidence)
        self.assertEqual(posture.state, "neutral")

    def test_a_missing_permission_is_unmet_and_names_the_credential(self):
        scope = _result(_posture(_groups(ability_available=False), self.request()), "scope")

        self.assertEqual(scope.state, Standing.UNMET)
        self.assertEqual(scope.reading.evidence, "Permission missing: Example")

    def test_missing_scope_or_untrusted_ingress_never_gets_a_green_summary(self):
        missing = _posture(_groups(ability_available=False), self.request())
        public = _posture(_groups(), self.request("203.0.113.7"))

        self.assertNotEqual(missing.state, "good")
        self.assertEqual(public.state, "serious")
        self.assertEqual(public.headline, "This connection needs a look.")

    def test_full_account_access_names_the_credentials(self):
        (group,) = _groups()
        view = group.connections[0]
        coarse = tuple(replace(state, evidence="coarse") for state in view.abilities * 3)
        views = (
            replace(view, abilities=coarse),
            replace(view, instance=replace(view.instance, label="Another"), abilities=coarse[:1]),
        )
        scope = _result(_posture((replace(group, connections=views),), self.request()), "scope")

        self.assertTrue(scope.reading.limited)
        self.assertIn("Another, Example", scope.reading.evidence)

    def test_a_connection_never_read_asks_to_be_checked(self):
        (group,) = _groups(observed=False)
        view = group.connections[0]
        read = replace(view, instance=replace(view.instance, observed_at=timezone.now()))

        for connections, state in (((view,), Standing.UNMET), ((read,), Standing.MET), ((), Standing.UNAVAILABLE)):
            with self.subTest(state=state):
                found = _posture((replace(group, connections=connections),), self.request())
                self.assertEqual(_result(found, "freshness").state, state)

    def test_a_keyless_public_service_is_not_owed_a_reading(self):
        (group,) = _groups()
        view = group.connections[0]
        keyless = replace(
            view, instance=replace(view.instance, label="Lookups", credential_model="none", observed_at=None)
        )
        # One that is read is as old as its reading, keyless or not.
        old = timezone.now() - timedelta(days=54)
        read = replace(keyless, instance=replace(keyless.instance, observed_at=old))

        found = _posture((replace(group, connections=(view, keyless)),), self.request())

        self.assertTrue(_result(found, "freshness").met)
        aged = _result(_posture((replace(group, connections=(view, read)),), self.request()), "freshness")
        self.assertEqual(aged.reading.evidence, f"Oldest reading {ago(old)}")

    def test_a_keyless_service_is_counted_once_and_alike_by_every_check(self):
        (group,) = _groups()
        view = group.connections[0]
        keyless = replace(
            view,
            instance=replace(view.instance, credential_model="none"),
            abilities=tuple(replace(state, evidence="not_applicable") for state in view.abilities),
        )

        keyless_group = replace(group, spec=replace(group.spec, secret_store=""), connections=(keyless,))
        found = _posture((keyless_group,), self.request())

        self.assertIn("1 need no key", _result(found, "credentials").reading.evidence)
        self.assertIn("1 need no key", _result(found, "scope").reading.evidence)

    def test_a_keyless_connection_needs_no_key_whatever_its_family_keeps(self):
        (group,) = _groups()
        view = group.connections[0]
        keyless = replace(
            view, instance=replace(view.instance, granted_scopes=(), scopes_known=False, credential_model="none")
        )

        custody = _result(_posture((replace(group, connections=(view, keyless)),), self.request()), "credentials")

        self.assertIn("1 in Example Vault", custody.reading.evidence)
        self.assertIn("1 need no key", custody.reading.evidence)
        self.assertTrue(custody.met)

    def test_npm_policy_is_derived_into_current_hostname_security(self):
        ProviderInventory.objects.create(
            kind="npm.proxy_host",
            observed_at=timezone.now(),
            records=[
                {
                    "domain_names": ["hq.example.test"],
                    "access_list_id": 7,
                    "access_policy": {
                        "name": "Tailnet only",
                        "satisfy_any": False,
                        "pass_auth": False,
                        "authorization_count": 0,
                        "clients": [
                            {"directive": "allow", "address": "100.64.0.0/10"},
                            {
                                "directive": "allow",
                                "address": "fd7a:115c:a1e0::/48",
                            },
                            {"directive": "deny", "address": "all"},
                        ],
                    },
                }
            ],
        )

        edge = observed_edge("hq.example.test")

        self.assertEqual(edge.standing, Standing.MET)
        self.assertEqual(edge.evidence, "Tailnet ranges\xa0· deny all")

    def test_npm_generated_final_deny_is_not_duplicated_to_prove_the_policy(self):
        ProviderInventory.objects.create(
            kind="npm.proxy_host",
            observed_at=timezone.now(),
            records=[
                {
                    "domain_names": ["hq.example.test"],
                    "access_list_id": 7,
                    "access_policy": {
                        "name": "Tailnet only",
                        "satisfy_any": False,
                        "pass_auth": False,
                        "authorization_count": 0,
                        "implicit_deny": True,
                        "clients": [
                            {"directive": "allow", "address": "100.64.0.0/10"},
                            {
                                "directive": "allow",
                                "address": "fd7a:115c:a1e0::/48",
                            },
                        ],
                    },
                }
            ],
        )

        edge = observed_edge("hq.example.test")

        self.assertEqual(edge.standing, Standing.MET)
        self.assertEqual(edge.evidence, "Tailnet ranges\xa0· implicit deny all")

    def test_a_widened_npm_policy_degrades_the_whole_posture(self):
        ProviderInventory.objects.create(
            kind="npm.proxy_host",
            observed_at=timezone.now(),
            records=[
                {
                    "domain_names": ["hq.example.test"],
                    "access_list_id": 7,
                    "access_policy": {
                        "name": "Too broad",
                        "satisfy_any": False,
                        "pass_auth": False,
                        "authorization_count": 0,
                        "clients": [{"directive": "allow", "address": "0.0.0.0/0"}],
                    },
                }
            ],
        )

        edge = observed_edge("hq.example.test")
        inputs = Inputs(edge=edge, firewall=Reading.unavailable("Not read"), groups=_groups())
        with override_settings(SEVERINO_POSTURE="advanced"):
            found = connection_security_posture(inputs, posture(inputs))

        self.assertEqual(edge.standing, Standing.UNMET)
        self.assertEqual(found.state, "serious")

    def test_the_proxy_and_the_firewall_share_the_estates_one_read(self):
        from ..connection_security import estate_readings

        ProviderInventory.objects.create(kind="npm.proxy_host", observed_at=timezone.now(), records=[])

        with projection_scope():
            stored_snapshots()
            with self.assertNumQueries(0):
                estate_readings("hq.example.test")


class TailnetPolicyTests(TestCase):
    """One check reads the tailnet policy, and judges it as well as reading it."""

    def policy(self, **record):
        ProviderInventory.objects.create(
            kind="tailscale.policy", observed_at=timezone.now(), records=[{"record": "policy", **record}]
        )

    def reading(self):
        from ..connection_security import tailnet_policy_reading

        return tailnet_policy_reading()

    def test_a_policy_read_is_met_and_counted(self):
        self.policy(grants=[{"src": ["group:staff"]}], tests=[{"src": "example@example.test"}])

        found = self.reading()
        self.assertEqual(found.standing, Standing.MET)
        self.assertEqual(found.evidence, "1 rule\xa0· 1 test")

    def test_an_allow_all_rule_is_unmet_although_read(self):
        self.policy(grants=[{"src": ["*"], "dst": ["*"], "ip": ["*"]}])

        self.assertEqual(self.reading().standing, Standing.UNMET)

    def test_a_policy_never_read_is_not_a_verdict(self):
        self.assertEqual(self.reading().standing, Standing.UNAVAILABLE)


@override_settings(SEVERINO_ENFORCE_TRUSTED_NETWORK=True)
class OneAnswerOnBothPagesTests(TestCase):
    """The Connections page answers each of the request's checks as the
    connection panel draws it, since both read the one result."""

    def test_a_plain_http_visit_over_the_tailnet_reads_alike_on_both_pages(self):
        self.client.force_login(get_user_model().objects.create_superuser("reader"))
        visit = arrived(REMOTE_ADDR="100.64.0.5")
        listed = self.client.get(reverse("control_plane:connections"), **visit).content.decode()
        panel = self.client.get(reverse("connection"), **visit).content.decode()

        drawn = {
            # The rung's own state, nearest first: a check's rung sits in its hop's.
            rung.attrs["data-connection-layer"]: next(
                ancestor.attrs["data-state"] for ancestor in reversed(rung.ancestors) if "data-state" in ancestor.attrs
            )
            for rung in select(panel, "data-connection-layer")
        }
        answered = {
            section.attrs["data-connection-control"]: section.words
            for section in select(listed, "data-connection-control")
        }
        said = {"met": "Yes", "unmet": "No", "unavailable": "Unknown"}
        self.assertIn("transport", answered.keys() & drawn.keys())
        for check_id in answered.keys() & drawn.keys():
            with self.subTest(check=check_id):
                self.assertIn(said[drawn[check_id]], answered[check_id].split())


class ArrivalInterfaceTests(TestCase):
    """The strongest claim on the page must not be the least evidenced one.

    "The address is on the tailnet" reads a field the sender writes. This
    reading says whether the kernel also required the packet to arrive there,
    so its absence has to read as unknown rather than as either answer.
    """

    def _reading(self, **overrides):
        record = {
            "record": "interface-binding",
            "interface": "tailscale0",
            "accept_requires_interface": True,
            "foreign_interface_dropped": True,
        }
        record.update(overrides)
        ProviderInventory.objects.create(
            kind="host.firewall",
            observed_at=timezone.now(),
            records=[record],
            reachable=True,
        )

    def test_a_reading_nothing_took_is_not_a_verdict(self):
        control = observed_firewall()
        self.assertEqual(control.standing, Standing.UNAVAILABLE)

    def test_an_unreachable_reading_is_not_a_verdict_either(self):
        ProviderInventory.objects.create(
            kind="host.firewall",
            observed_at=timezone.now(),
            records=[{"record": "interface-binding"}],
            reachable=False,
        )
        self.assertEqual(observed_firewall().standing, Standing.UNAVAILABLE)

    def test_the_interface_being_required_is_what_makes_it_good(self):
        self._reading()
        control = observed_firewall()
        self.assertEqual(control.standing, Standing.MET)
        self.assertIn("tailscale0", control.evidence)
        self.assertIn("drops a tailnet address", control.detail)

    def test_admitting_on_the_address_alone_is_reported_as_such(self):
        self._reading(accept_requires_interface=False, foreign_interface_dropped=False)
        control = observed_firewall()
        self.assertEqual(control.standing, Standing.UNMET)
        self.assertIn("address alone", control.detail)

    def test_accepting_without_dropping_says_what_is_uncounted(self):
        self._reading(foreign_interface_dropped=False)
        control = observed_firewall()
        self.assertEqual(control.standing, Standing.MET)
        self.assertIn("has no rule that drops", control.detail)


class IngressRegistryTests(TestCase):
    """The ingress reading asks the registry which kinds carry a source policy."""

    def test_any_kind_declaring_an_ingress_policy_is_read(self):
        from unittest import mock

        from hq.domains.control_plane.provider_adapters.contracts import IngressPolicy
        from hq.domains.control_plane.providers import PROVIDERS

        proxy = replace(
            PROVIDERS["npm.proxy_host"],
            kind="example.proxy",
            connection_providers=("ssh",),
            ingress_policy=lambda record: IngressPolicy(hostnames=tuple(record["names"]), restricted=False),
        )
        ProviderInventory.objects.create(
            kind="example.proxy",
            observed_at=timezone.now(),
            reachable=True,
            records=[{"names": ["hq.example.test"]}],
        )
        with mock.patch.dict(PROVIDERS, {"example.proxy": proxy}):
            edge = observed_edge("hq.example.test")

        self.assertEqual(edge.standing, Standing.UNMET)
        self.assertEqual(edge.evidence, "No access list")
        self.assertIn("SSH", edge.detail)
