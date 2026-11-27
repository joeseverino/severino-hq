"""A 500 carries the request id its log does; a stale form is refused without HQ's internals."""

from django.test import Client, RequestFactory, SimpleTestCase, TestCase

from hq.platform.application.markup_testing import text, texts
from hq.platform.core.error_views import server_error
from hq.platform.core.testing import operator


class ServerErrorTests(SimpleTestCase):
    def test_a_500_gives_the_request_id_the_log_carries(self):
        request = RequestFactory().get("/calendar/")
        self.assertEqual(server_error(request).status_code, 500)

        request.request_id = "0123abcd"

        self.assertIn("0123abcd", texts(server_error(request).content.decode(), "code"))


class RefusalTests(TestCase):
    def setUp(self):
        self.user = operator(username="reader")

    def test_a_stale_form_is_answered_on_the_same_page_and_says_so(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)

        response = client.post("/theme/", {"theme": "dark"})

        self.assertEqual(response.status_code, 403)
        self.assertNotIn("CSRF", text(response))
