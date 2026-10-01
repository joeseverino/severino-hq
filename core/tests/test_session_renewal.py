"""Renewing a session returns the operator to the page they were on."""

from __future__ import annotations

from unittest import mock

from django.contrib.sessions.backends.signed_cookies import SessionStore
from django.http import HttpResponse, JsonResponse
from django.test import RequestFactory, SimpleTestCase, override_settings

from core.oidc import HQSessionRefresh


@override_settings(ALLOWED_HOSTS=["hq.example.com"])
class RenewalTests(SimpleTestCase):
    def renew(self, path, *, background, referer=None):
        headers = {"HTTP_HOST": "hq.example.com"}
        if background:
            headers["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
        if referer:
            headers["HTTP_REFERER"] = referer
        request = RequestFactory().get(path, secure=True, **headers)
        request.session = SessionStore()

        def renewing(_self, request):
            request.session["oidc_login_next"] = request.get_full_path()
            return JsonResponse({}, status=403) if background else HttpResponse(status=302)

        with mock.patch("mozilla_django_oidc.middleware.SessionRefresh.process_request", renewing):
            HQSessionRefresh(lambda request: HttpResponse()).process_request(request)
        return request.session["oidc_login_next"]

    def test_a_background_request_returns_to_the_page_it_serves(self):
        self.assertEqual(
            self.renew("/health/live/?t=1", background=True,
                       referer="https://hq.example.com/infrastructure/connections/?kind=dns"),
            "/infrastructure/connections/?kind=dns",
        )

    def test_a_background_request_from_another_site_returns_home(self):
        self.assertEqual(
            self.renew("/api/count/", background=True, referer="https://evil.example.net/x"),
            "/",
        )
        self.assertEqual(self.renew("/api/count/", background=True), "/")

    def test_a_page_the_operator_opened_returns_to_itself(self):
        self.assertEqual(self.renew("/projects/?q=hq", background=False), "/projects/?q=hq")
