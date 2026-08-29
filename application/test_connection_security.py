"""The connection page's security story must stay derived and honest."""

from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from control_plane.models import ProviderInventory

from .connection_security import (
    connection_security_posture,
    observed_connection_controls,
    observed_ingress_control,
)
from .connections import (
    ConnectionAbility,
    ConnectionAbilityState,
    ConnectionGroup,
    ConnectionInstance,
    ConnectionLink,
    ConnectionSpec,
    ConnectionView,
)
from .security import Capability


def _groups(*, ability_available=True, status="good", required_scopes=("example:read",)):
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
                    (
                        ConnectionAbilityState(
                            ability,
                            ability_available,
                            () if ability_available is not False else ("example:read",),
                        ),
                    ),
                ),
            ),
        ),
    )


@override_settings(SEVERINO_ENFORCE_TRUSTED_NETWORK=True)
class ConnectionSecurityPostureTests(TestCase):
    def request(self, address="100.64.0.5", *, secure=True, **extra):
        return RequestFactory().get(
            "/infrastructure/connections/",
            secure=secure,
            HTTP_HOST="hq.example.test",
            REMOTE_ADDR=address,
            **extra,
        )

    def test_tailnet_ingress_and_connection_authority_are_derived_together(self):
        posture = connection_security_posture(_groups(), request=self.request())

        self.assertEqual(posture.headline, "Tailnet ingress. Explicit authority.")
        self.assertEqual(posture.external_custody_count, 1)
        self.assertEqual(posture.scope_verified_count, 1)
        self.assertEqual(posture.dependency_count, 1)
        self.assertEqual(
            next(control for control in posture.controls if control.id == "edge").state,
            "neutral",
        )

    @override_settings(SEVERINO_TRUSTED_PROXIES=["10.0.0.9/32"])
    def test_the_current_trusted_proxy_path_is_proven_without_a_probe(self):
        posture = connection_security_posture(
            _groups(),
            request=self.request("10.0.0.9", HTTP_X_FORWARDED_FOR="100.64.0.5"),
        )

        proxy = next(control for control in posture.controls if control.id == "proxy")
        self.assertEqual(proxy.state, "good")
        self.assertEqual(proxy.evidence, "1 trusted proxy hop")

    def test_unknown_scope_evidence_stays_visibly_unknown(self):
        posture = connection_security_posture(
            _groups(ability_available=None), request=self.request()
        )

        scope = next(control for control in posture.controls if control.id == "scope")
        self.assertEqual(scope.state, "neutral")
        self.assertIn("1 unknown", scope.evidence)
        self.assertEqual(posture.state, "neutral")

    def test_scope_free_abilities_are_capability_only_not_unknown(self):
        posture = connection_security_posture(
            _groups(ability_available=True, required_scopes=()), request=self.request()
        )

        scope = next(control for control in posture.controls if control.id == "scope")
        self.assertIn("1 capability-only", scope.evidence)
        self.assertIn("0 unknown", scope.evidence)
        self.assertEqual(posture.scope_capability_only_count, 1)
        self.assertEqual(posture.scope_unknown_count, 0)

    def test_missing_scope_or_untrusted_ingress_never_gets_a_green_summary(self):
        missing = connection_security_posture(
            _groups(ability_available=False), request=self.request()
        )
        public = connection_security_posture(
            _groups(), request=self.request("203.0.113.7")
        )

        self.assertEqual(missing.state, "serious")
        self.assertEqual(public.state, "serious")

    def test_deriving_the_posture_costs_no_queries(self):
        with self.assertNumQueries(0):
            connection_security_posture(_groups(), request=self.request())

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

        with self.assertNumQueries(1):
            edge = observed_ingress_control("hq.example.test")
        posture = connection_security_posture(
            _groups(), request=self.request(), edge=edge
        )

        self.assertEqual(edge.state, "good")
        self.assertEqual(edge.evidence, "Tailnet ranges · deny all")
        self.assertEqual(posture.state, "good")

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

        edge = observed_ingress_control("hq.example.test")

        self.assertEqual(edge.state, "good")
        self.assertEqual(edge.evidence, "Tailnet ranges · implicit deny all")

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

        edge = observed_ingress_control("hq.example.test")
        posture = connection_security_posture(
            _groups(), request=self.request(), edge=edge
        )

        self.assertEqual(edge.state, "serious")
        self.assertEqual(posture.state, "serious")

    def test_tailscale_policy_and_npm_edge_share_one_cached_read(self):
        ProviderInventory.objects.bulk_create(
            [
                ProviderInventory(
                    kind="tailscale.policy",
                    observed_at=timezone.now(),
                    records=[
                        {
                            "record": "policy",
                            "grants": [{"src": ["group:staff"]}],
                            "tests": [{"src": "example@example.test"}],
                        }
                    ],
                ),
                ProviderInventory(
                    kind="npm.proxy_host",
                    observed_at=timezone.now(),
                    records=[],
                ),
            ]
        )

        with self.assertNumQueries(1):
            tailnet_policy, _edge = observed_connection_controls(
                "hq.example.test"
            )

        self.assertEqual(tailnet_policy.state, "good")
        self.assertEqual(tailnet_policy.evidence, "Observed · 1 grant · 1 test")
