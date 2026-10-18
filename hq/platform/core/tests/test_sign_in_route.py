"""Where an expired session is sent is the login route, said once."""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.platform.application.tests.markup import elements


class SignInRouteTests(TestCase):
    def test_the_route_and_the_redirects_are_the_setting(self):
        self.assertEqual(reverse("login"), settings.LOGIN_URL)
        self.assertTrue(settings.LOGOUT_REDIRECT_URL.startswith(f"{settings.LOGIN_URL}?"))
        self.assertTrue(settings.LOGIN_REDIRECT_URL_FAILURE.startswith(f"{settings.LOGIN_URL}?"))

    def test_every_page_tells_its_scripts_where_signing_in_is(self):
        self.client.force_login(get_user_model().objects.create_user("owner", password="unused-password"))

        response = self.client.get(reverse("dashboard"))

        (body,) = elements(response, "body")
        self.assertEqual(body.attrs["data-sign-in"], settings.LOGIN_URL)
