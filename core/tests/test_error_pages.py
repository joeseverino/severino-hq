"""The error pages say whose they are."""

from django.test import RequestFactory, SimpleTestCase

from core.error_views import server_error


class ServerErrorTests(SimpleTestCase):
    def test_a_500_names_hq_without_the_context_that_may_have_failed(self):
        response = server_error(RequestFactory().get("/calendar/"))
        body = response.content.decode()
        self.assertEqual(response.status_code, 500)
        self.assertIn("Something broke · Severino HQ</title>", body)
        self.assertIn('class="brand"', body)
        self.assertIn("Severino HQ · Unauthorized Access", body)
