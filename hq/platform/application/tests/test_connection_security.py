"""The connections page's security story is the standards' checks, measured once."""

from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.platform.core.tests.forwarding import arrived

from ..connection_catalog import ConnectionAbilityState, ConnectionGroup, ConnectionView
from ..connection_security import connection_security_posture
from ..connections import (
    ConnectionAbility,
    ConnectionInstance,
    ConnectionLink,
    ConnectionSpec,
)
from ..posture import Inputs, posture
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


def _groups():
    ability = ConnectionAbility(
        "example.read", "Read example", "Read one synthetic account.", required_scopes=("example:read",)
    )
    instance = ConnectionInstance(
        "example:one",
        "Example",
        "example",
        "good",
        "healthy",
        scopes_known=True,
        granted_scopes=("example:read",),
        ability_names=(ability.name,),
        dependencies=(ConnectionLink("example.resource"),),
        credential_model="scoped",
        observed_at=timezone.now(),
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
    state = ConnectionAbilityState(ability, available=True, missing_scopes=(), action=None, evidence="verified")
    return (ConnectionGroup(spec, (ConnectionView(instance, lifecycle="reachable", abilities=(state,)),)),)


class OneStateModelTests(TestCase):
    """The headline counts the lifecycle each row shows, so they cannot disagree."""

    def test_without_a_request_the_posture_is_the_estate_alone(self):
        posture = _posture(_groups())

        self.assertEqual(posture.headline, "")
        self.assertEqual(posture.admission, ())
        self.assertEqual(posture.state, "good")


@override_settings(SEVERINO_ENFORCE_TRUSTED_NETWORK=True, ALLOWED_HOSTS=["hq.example.test"])
class ConnectionSecurityPostureTests(TestCase):
    def request(self, address="100.64.0.5", *, secure=True, **extra):
        return RequestFactory().get(
            "/infrastructure/connections/",
            secure=secure,
            HTTP_HOST="hq.example.test",
            **arrived(REMOTE_ADDR=address, **extra),
        )

    def test_plain_http_over_the_tailnet_is_encrypted_by_wireguard(self):
        posture = _posture(_groups(), self.request(secure=False))

        self.assertTrue(_result(posture, "transport").met)
        self.assertIn("tailnet", posture.headline)

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


class TailnetPolicyTests(TestCase):
    """One check reads the tailnet policy, and judges it as well as reading it."""

    def policy(self, **record):
        ProviderInventory.objects.create(
            kind="tailscale.policy", observed_at=timezone.now(), records=[{"record": "policy", **record}]
        )

    def reading(self):
        from ..connection_security import tailnet_policy_reading

        return tailnet_policy_reading()

    def test_an_allow_all_rule_is_unmet_although_read(self):
        self.policy(grants=[{"src": ["*"], "dst": ["*"], "ip": ["*"]}])

        self.assertEqual(self.reading().standing, Standing.UNMET)


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

    def test_admitting_on_the_address_alone_is_reported_as_such(self):
        self._reading(accept_requires_interface=False, foreign_interface_dropped=False)
        control = observed_firewall()
        self.assertEqual(control.standing, Standing.UNMET)

    def test_accepting_without_dropping_says_what_is_uncounted(self):
        self._reading(foreign_interface_dropped=False)
        control = observed_firewall()
        self.assertEqual(control.standing, Standing.MET)
        self.assertIn("has no rule that drops", control.detail)
