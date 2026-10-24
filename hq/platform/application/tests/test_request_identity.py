from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, TestCase

from hq.platform.application.request_context import from_scope
from hq.platform.application.request_identity import signed_in


class SignedInTests(TestCase):
    def test_a_session_user_is_signed_in(self):
        request = RequestFactory().get("/")
        request.user = get_user_model().objects.create_user("example-operator")
        self.assertEqual(signed_in(request), request.user)

    def test_an_anonymous_user_is_not(self):
        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        self.assertIsNone(signed_in(request))

    def test_a_request_no_session_reached_is_not(self):
        request = from_scope({"type": "http", "method": "GET", "path": "/", "headers": []}, allowed_hosts=())
        self.assertIsNone(signed_in(request))
