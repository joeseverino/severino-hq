"""A person registers a client as their own device, for themselves only, and nothing else does."""

from django.conf import settings
from django.test import TestCase
from django.urls import reverse

from hq.platform.application.plugin_testing import form_keyed
from hq.platform.application.tests.writers import keyed
from hq.platform.core.models import OwnDevice
from hq.platform.core.testing import operator

from ..capabilities import execute_capability
from ..linked_accounts import bind_sign_in, sign_in_subject
from ..own_devices import SET_OWN_DEVICE
from ..security import web_principal

PHONE = "example-phone"


def _person(name: str, subject: str):
    user = operator(username=name)
    bind_sign_in(user, sign_in_subject(settings.OIDC_ISSUER, subject))
    return user


class RegistrationTests(TestCase):
    def setUp(self):
        self.person = _person("op", "example-subject")

    def _set(self, *, state: bool, client: str = PHONE, user=None):
        return execute_capability(
            SET_OWN_DEVICE, keyed({"registered": state}), principal=web_principal(user or self.person), target=client
        )

    def test_a_person_with_no_provider_sign_in_registers_nothing(self):
        unbound = operator()

        result = self._set(state=True, user=unbound)

        self.assertEqual(result["error"]["code"], "precondition")
        self.assertFalse(OwnDevice.objects.exists())


class DeviceControlTests(TestCase):
    def setUp(self):
        self.person = _person("op", "example-subject")
        self.client.force_login(self.person)

    def _post(self, **fields):
        return self.client.post(reverse("own_device"), form_keyed(fields), follow=True)

    def test_a_form_missing_its_state_or_client_registers_nothing(self):
        self.assertEqual(self.client.post(reverse("own_device"), form_keyed({"registered": "true"})).status_code, 400)
        self._post(__target=PHONE)
        self.assertFalse(OwnDevice.objects.exists())
