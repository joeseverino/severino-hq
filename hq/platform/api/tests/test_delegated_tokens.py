"""A token an agent's client got through a person's sign-in is that agent's, acting for that person.

Pocket ID names the registered client in ``client_id`` on every token, and on
a grant a person allowed (authorization code, device flow) it names that
person in ``sub``. The scope is the client's own user-delegated grant, never
the person's permissions. So such a token is an agent: its client is the
actor, its grant is capped and paused as every agent's is, and what it keeps
(settings, pins, set-asides) is its person's, though it never stands as them.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from hq.platform.application.linked_accounts import bind_sign_in, sign_in_subject
from hq.platform.application.security import DEVICE_INTERFACE, is_interactive, person_of

from .tokens import ISSUER, RESOURCE, _serving, _token

# A person's user id as Pocket ID writes it into `sub`, and an agent's client.
PERSON = "3f1c2b7e-5a4d-4e8b-9c1a-0d2e3f4a5b6c"
AGENT_CLIENT = "example-agent"


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
)
class DelegatedTokenTests(TestCase):
    def setUp(self):
        # The person has signed in to HQ, so a `sub` naming them names somebody.
        self.person = get_user_model().objects.create_user(username="operator", password="test-only-password")
        bind_sign_in(self.person, sign_in_subject(ISSUER, PERSON))

    def _admitted(self, scope, interface):
        from hq.platform.application.agent_admission import admit

        token = _token(AGENT_CLIENT, sub=PERSON, scope=scope)
        with _serving():
            return admit(token, interface=interface, source="")

    def test_it_is_an_agent_with_its_clients_own_grant_on_both_doors(self):
        for interface in ("api", "mcp"):
            with self.subTest(interface=interface):
                principal = self._admitted("read write_projects", interface)

                self.assertEqual((principal.actor, principal.interface), (AGENT_CLIENT, interface))
                self.assertEqual(principal.granted, frozenset({"read", "write_projects"}))
                # Its grant, within the role of the person who signed it in.
                self.assertTrue(principal.permits("read", "write_projects"))

    def test_it_acts_for_its_person_without_standing_as_them(self):
        principal = self._admitted("read decide_approvals govern_agents", "api")

        self.assertFalse(is_interactive(principal))
        self.assertEqual(person_of(principal), self.person)
        # What only a person may do stays out of its reach, whatever it was granted.
        self.assertFalse(principal.permits("decide_approvals"))
        self.assertFalse(principal.permits("govern_agents"))

    def test_the_pause_stops_it(self):
        from hq.platform.application.agent_access import AGENTS_PAUSED, refusal
        from hq.platform.core.models import AgentAccess

        AgentAccess.objects.update_or_create(pk=1, defaults={"paused": True})

        self.assertEqual(refusal(self._admitted("read", "mcp")), AGENTS_PAUSED)

    def _registered(self, user):
        from hq.platform.core.models import OwnDevice

        OwnDevice.objects.create(user=user, client_id=AGENT_CLIENT)

    def test_the_providers_device_grant_alone_leaves_it_an_agent(self):
        """A grant given in the provider by mistake promotes no client the person did not register."""

        for interface in ("api", "mcp"):
            with self.subTest(interface=interface):
                principal = self._admitted("read own_device", interface)

                self.assertEqual(principal.interface, interface)
                self.assertFalse(is_interactive(principal))
                self.assertFalse(principal.permits("own_device"))

    def test_a_client_the_person_registered_is_their_device(self):
        self._registered(self.person)

        principal = self._admitted("read own_device", "mcp")

        self.assertEqual(principal.interface, DEVICE_INTERFACE)
        self.assertEqual(person_of(principal), self.person)

    def test_a_registration_made_by_another_person_grants_nothing(self):
        other = get_user_model().objects.create_user(username="someone-else", password="test-only-password")
        bind_sign_in(other, sign_in_subject(ISSUER, "7a9e0c1d-2b3f-4c5d-8e6f-1a2b3c4d5e6f"))
        self._registered(other)

        principal = self._admitted("read own_device", "mcp")

        self.assertEqual(principal.interface, "mcp")
        # It acts for whoever signed it in, never for whoever registered it.
        self.assertEqual(person_of(principal), self.person)

    def test_a_registration_grants_nothing_without_the_providers_grant(self):
        self._registered(self.person)

        self.assertEqual(self._admitted("read", "mcp").interface, "mcp")
