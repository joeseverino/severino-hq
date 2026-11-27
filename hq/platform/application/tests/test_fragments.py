"""A page answers one named part of itself, and "unchanged" when it is."""

from django.test import RequestFactory, TestCase
from django.urls import reverse

from hq.platform.application import fragments
from hq.platform.application.markup_testing import elements
from hq.platform.core.testing import SignedInTestCase, operator


class PagePartTests(SignedInTestCase):
    def test_saving_the_links_is_answered_with_the_links(self):
        response = self.client.post(
            reverse("dashboard_links"), {"keys": []}, headers={"X-Fragment": "links"}, follow=True
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(elements(response, "html"), [])


class UnchangedPartTests(TestCase):
    def setUp(self):
        self.user = operator(self.client)
        self.url = reverse("dashboard_glance")

    def test_a_validator_stops_holding_at_its_moment(self):
        request = RequestFactory().get(self.url, headers={"If-None-Match": f'"{"a" * 64}-1"'})

        self.assertIsNone(fragments.presented(request, "a" * 64))

    def test_an_unknown_input_is_never_unchanged(self):
        request = RequestFactory().get(self.url)
        request.user = self.user

        self.assertIsNone(fragments.base(request, None))
        self.assertIsNone(fragments.standing(None))
        self.assertIsNone(fragments.presented(request, None))
