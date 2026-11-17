"""Where an expired session is sent is the login route, said once."""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.platform.application.markup_testing import elements


class SignInRouteTests(TestCase):
    def test_every_page_tells_its_scripts_where_signing_in_is(self):
        self.client.force_login(get_user_model().objects.create_user("owner", password="unused-password"))

        response = self.client.get(reverse("dashboard"))

        (body,) = elements(response, "body")
        self.assertEqual(body.attrs["data-sign-in"], settings.LOGIN_URL)
