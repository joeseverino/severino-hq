"""Where an expired session is sent is the login route, said once."""

from django.conf import settings
from django.test import TestCase
from django.urls import reverse

from hq.platform.application.markup_testing import elements
from hq.platform.core.testing import operator


class SignInRouteTests(TestCase):
    def test_every_page_tells_its_scripts_where_signing_in_is(self):
        operator(self.client)

        response = self.client.get(reverse("dashboard"))

        (body,) = elements(response, "body")
        self.assertEqual(body.attrs["data-sign-in"], settings.LOGIN_URL)
