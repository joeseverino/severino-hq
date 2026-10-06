"""The error pages say whose they are, in the site's frame."""

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import path

from hq.config.urls import urlpatterns as site_urls
from hq.platform.core.error_views import server_error


def refused(request):
    raise PermissionDenied("Requires manage_example.")


urlpatterns = [path("example/refused/", refused), *site_urls]


class ServerErrorTests(SimpleTestCase):
    def test_a_500_names_hq_without_the_context_that_may_have_failed(self):
        response = server_error(RequestFactory().get("/calendar/"))
        body = response.content.decode()
        self.assertEqual(response.status_code, 500)
        self.assertIn("Something broke · Severino HQ</title>", body)
        self.assertIn('class="brand"', body)
        self.assertIn("Trying again is safe.", body)
        self.assertNotIn("request id", body)

    def test_a_500_gives_the_request_id_the_log_carries(self):
        request = RequestFactory().get("/calendar/")
        request.request_id = "0123abcd"

        body = server_error(request).content.decode()

        self.assertIn("under request id <code>0123abcd</code>", body)


@override_settings(ROOT_URLCONF=__name__)
class RefusalTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="reader")

    def test_a_refused_page_is_hqs_own_and_keeps_the_permission_name_to_itself(self):
        self.client.force_login(self.user)

        response = self.client.get("/example/refused/")

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Not allowed · Severino HQ</title>", status_code=403)
        self.assertContains(response, "You can't open this", status_code=403)
        self.assertContains(response, 'class="brand"', status_code=403)
        self.assertNotContains(response, "manage_example", status_code=403)

    def test_a_stale_form_is_answered_on_the_same_page_and_says_so(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)

        response = client.post("/theme/", {"theme": "dark"})

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "That form is out of date", status_code=403)
        self.assertContains(response, "Nothing was saved.", status_code=403)
        self.assertNotContains(response, "CSRF", status_code=403)


class NotFoundTests(TestCase):
    def test_a_404_offers_the_search_to_a_signed_in_reader_only(self):
        missing = "/no-such-page/"
        anonymous = self.client.get(missing)
        self.client.force_login(get_user_model().objects.create_user(username="reader"))
        signed_in = self.client.get(missing)

        if anonymous.status_code == 404:
            self.assertNotContains(anonymous, 'placeholder="Find anything"', status_code=404)
        self.assertContains(signed_in, 'placeholder="Find anything"', status_code=404)
        self.assertContains(signed_in, "That page isn't here", status_code=404)
