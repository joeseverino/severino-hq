"""A person registers a client as their own device, for themselves only, and nothing else does."""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.platform.application.plugin_testing import form_keyed
from hq.platform.application.tests.writers import keyed
from hq.platform.core.models import AuditLog, OwnDevice

from ..capabilities import execute_capability
from ..linked_accounts import bind_sign_in, sign_in_subject
from ..own_devices import SET_OWN_DEVICE, registered
from ..security import Capability, Principal, cli_principal, web_principal

PHONE = "example-phone"


def _person(name: str, subject: str):
    user = get_user_model().objects.create_user(name, password="x" * 20)
    bind_sign_in(user, sign_in_subject(settings.OIDC_ISSUER, subject))
    return user


class RegistrationTests(TestCase):
    def setUp(self):
        self.person = _person("op", "example-subject")
        self.key = sign_in_subject(settings.OIDC_ISSUER, "example-subject")

    def _set(self, *, state: bool, client: str = PHONE, user=None):
        return execute_capability(
            SET_OWN_DEVICE, keyed({"registered": state}), principal=web_principal(user or self.person), target=client
        )

    def test_a_registration_stands_for_the_person_who_made_it_alone(self):
        other = _person("someone", "example-other-subject")

        self.assertTrue(self._set(state=True)["ok"])

        self.assertTrue(registered(PHONE, self.key))
        self.assertFalse(registered(PHONE, sign_in_subject(settings.OIDC_ISSUER, "example-other-subject")))
        self.assertFalse(OwnDevice.objects.filter(user=other).exists())

    def test_removing_ends_it_and_both_are_recorded_once(self):
        self._set(state=True)
        self._set(state=True)
        self._set(state=False)
        self._set(state=False)

        self.assertFalse(registered(PHONE, self.key))
        events = AuditLog.objects.filter(metadata__operation=SET_OWN_DEVICE).order_by("id")
        self.assertEqual([event.action for event in events], [AuditLog.Action.CREATED, AuditLog.Action.DELETED])
        self.assertEqual({event.user for event in events}, {self.person})

    def test_a_client_the_provider_could_not_have_registered_is_refused(self):
        for client in ("https://agent.example.com/client.json", "two words", ""):
            with self.subTest(client=client):
                self.assertFalse(self._set(state=True, client=client)["ok"])
        self.assertFalse(OwnDevice.objects.exists())

    def test_a_person_with_no_provider_sign_in_registers_nothing(self):
        unbound = get_user_model().objects.create_user("local", password="x" * 20)

        result = self._set(state=True, user=unbound)

        self.assertEqual(result["error"]["code"], "precondition")
        self.assertFalse(OwnDevice.objects.exists())

    def test_no_credential_registers_a_device(self):
        granted = frozenset(Capability)
        for credential in (
            Principal("example-agent", "mcp", granted, granted=granted),
            Principal("example-client", "api", granted, granted=granted),
            cli_principal(),
        ):
            with self.subTest(interface=credential.interface):
                result = execute_capability(
                    SET_OWN_DEVICE, keyed({"registered": True}), principal=credential, target="example-agent"
                )

                self.assertEqual(result["error"]["code"], "forbidden")
        self.assertFalse(OwnDevice.objects.exists())


class DeviceControlTests(TestCase):
    def setUp(self):
        self.person = _person("op", "example-subject")
        self.client.force_login(self.person)

    def _post(self, **fields):
        return self.client.post(reverse("own_device"), form_keyed(fields), follow=True)

    def test_the_page_registers_and_removes_the_signed_in_persons_device(self):
        self._post(client_id=PHONE, registered="1")
        self.assertEqual(list(OwnDevice.objects.values_list("user", "client_id")), [(self.person.pk, PHONE)])

        self._post(client_id=PHONE, registered="0")
        self.assertFalse(OwnDevice.objects.exists())

    def test_a_refusal_is_said_on_the_page(self):
        response = self._post(client_id="https://agent.example.com/client.json", registered="1")

        self.assertIn("client id", response.content.decode())
        self.assertFalse(OwnDevice.objects.exists())

    def test_a_form_missing_its_state_or_client_changes_nothing(self):
        self.assertEqual(self.client.post(reverse("own_device"), form_keyed({"client_id": PHONE})).status_code, 400)
        self.assertEqual(self.client.post(reverse("own_device"), form_keyed({"registered": "1"})).status_code, 400)
        self.assertFalse(OwnDevice.objects.exists())
