"""The connection page's security story must stay derived and honest."""

from django.test import RequestFactory, TestCase, override_settings

from .connection_security import connection_security_posture
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


def _groups(*, ability_available=True, status="good"):
    ability = ConnectionAbility(
        "example.read",
        "Read example",
        "Read one synthetic account.",
        required_scopes=("example:read",),
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
            request=self.request(
                "10.0.0.9", HTTP_X_FORWARDED_FOR="100.64.0.5"
            ),
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
