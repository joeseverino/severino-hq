"""Renewing a session returns the operator to the page they were on."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.backends.signed_cookies import SessionStore
from django.http import HttpResponse, JsonResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from hq.platform.core.oidc import HQSessionRefresh


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

    def test_the_probes_never_renew(self):
        exempt = HQSessionRefresh(lambda request: HttpResponse()).exempt_urls
        self.assertLessEqual({"/health/live/", "/health/ready/"}, exempt)


@override_settings(
    ALLOWED_HOSTS=["hq.example.com"],
    AUTHENTICATION_BACKENDS=["hq.platform.core.oidc.HQOIDCAuthenticationBackend"],
)
class RenamedBackendTests(SimpleTestCase):
    """A session from before a backend was renamed ends; it is not an error."""

    def request(self, backend):
        request = RequestFactory().get("/", secure=True, HTTP_HOST="hq.example.com")
        request.session = SessionStore()
        request.session["_auth_user_id"] = "1"
        request.session["_auth_user_backend"] = backend
        request.session["oidc_id_token_expiration"] = 0
        # What Django's own middleware makes of a backend it does not have.
        request.user = AnonymousUser()
        return request

    def test_a_session_naming_a_backend_that_is_gone_is_ended(self):
        request = self.request("core.oidc.HQOIDCAuthenticationBackend")
        # The real refresh, which imports whatever backend the session names.
        HQSessionRefresh(lambda request: HttpResponse()).process_request(request)
        self.assertNotIn("_auth_user_backend", request.session)
        self.assertNotIn("_auth_user_id", request.session)

    def test_a_session_naming_the_current_backend_is_kept(self):
        request = self.request("hq.platform.core.oidc.HQOIDCAuthenticationBackend")
        with mock.patch(
            "mozilla_django_oidc.middleware.SessionRefresh.process_request", return_value=None
        ):
            HQSessionRefresh(lambda request: HttpResponse()).process_request(request)
        self.assertEqual(request.session["_auth_user_id"], "1")


class ExpiredSessionPurgeTests(TestCase):
    """A session row outlives its expiry until the daily job deletes it."""

    def test_the_scheduled_job_deletes_sessions_past_their_expiry(self):
        from datetime import timedelta

        from django.contrib.sessions.models import Session
        from django.utils import timezone

        from hq.platform.application import scheduled_work

        now = timezone.now()
        Session.objects.create(session_key="expired", session_data="", expire_date=now - timedelta(hours=1))
        Session.objects.create(session_key="current", session_data="", expire_date=now + timedelta(hours=1))

        answer = scheduled_work.run("sessions.clear")

        self.assertEqual(answer["state"], "succeeded")
        self.assertEqual(list(Session.objects.values_list("session_key", flat=True)), ["current"])
