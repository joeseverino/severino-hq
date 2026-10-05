"""A page answers one named part of itself, and "unchanged" when it is."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse

from hq.platform.application import fragments

PART = {"X-Fragment": "calendar"}


class RequestedPartTests(TestCase):
    def test_only_a_template_identifier_names_a_part(self):
        factory = RequestFactory()
        for sent, read in (
            ("calendar", "calendar"),
            ("dashboard_links", "dashboard_links"),
            ("", ""),
            ("Calendar", ""),
            ("a#b", ""),
            ("../base", ""),
            ("x" * 80, ""),
        ):
            with self.subTest(sent=sent):
                request = factory.get("/", headers={"X-Fragment": sent})
                self.assertEqual(fragments.requested(request), read)

    def test_a_part_is_tried_before_its_page(self):
        request = RequestFactory().get("/", headers=PART)
        self.assertEqual(
            fragments.template_names(request, ["a.html"]), ["a.html#calendar", "a.html"]
        )
        self.assertEqual(fragments.template_names(RequestFactory().get("/"), ["a.html"]), ["a.html"])


class PagePartTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("operator", password="x" * 20)
        self.client.force_login(self.user)

    def test_a_page_answers_its_named_part_alone(self):
        url = reverse("calendar:month")
        page = self.client.get(url)
        part = self.client.get(url, headers=PART)

        self.assertContains(page, "<html")
        self.assertNotContains(part, "<html")
        self.assertContains(part, 'id="calendar"')
        self.assertLess(len(part.content), len(page.content))
        # One address, two documents: a cache is told which header chose.
        for response in (page, part):
            self.assertIn("X-Fragment", response["Vary"])

    def test_a_part_the_page_does_not_define_is_answered_with_the_page(self):
        response = self.client.get(reverse("calendar:month"), headers={"X-Fragment": "nothing"})

        self.assertContains(response, "<html")

    def test_the_dashboard_composes_only_the_part_asked_for(self):
        with patch(
            "hq.platform.core.dashboard_views.operating_snapshot",
            side_effect=AssertionError("a part composed the whole dashboard"),
        ):
            response = self.client.get("/?month=2026-01", headers=PART)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "<html")

    def test_saving_the_links_is_answered_with_the_links(self):
        response = self.client.post(
            reverse("dashboard_links"), {"href": []}, headers={"X-Fragment": "links"}, follow=True
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "<html")

    def test_the_connection_dialog_is_answered_with_the_panel(self):
        response = self.client.get(reverse("connection"), headers={"X-Fragment": "connection"})

        self.assertContains(response, "data-connection-panel")
        self.assertNotContains(response, "<html")

    def test_the_policy_test_is_answered_with_its_result(self):
        response = self.client.get(
            reverse("control_plane:tailnet"), headers={"X-Fragment": "whatif"}
        )

        self.assertContains(response, 'id="whatif-result"')
        self.assertNotContains(response, "<html")


class UnchangedPartTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("operator", password="x" * 20)
        self.client.force_login(self.user)
        self.url = reverse("dashboard_glance")

    def test_a_poll_that_finds_nothing_written_is_answered_304(self):
        first = self.client.get(self.url)
        validator = first["ETag"]
        self.assertEqual(first["Cache-Control"], "private, no-cache")

        with patch(
            "hq.platform.core.dashboard_views.glance_context",
            side_effect=AssertionError("an unchanged strip was composed"),
        ):
            again = self.client.get(self.url, headers={"If-None-Match": validator})

        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.content, b"")

    def test_any_write_ends_it(self):
        validator = self.client.get(self.url)["ETag"]
        get_user_model().objects.create_user("another", password="y" * 20)

        again = self.client.get(self.url, headers={"If-None-Match": validator})

        self.assertEqual(again.status_code, 200)
        self.assertNotEqual(again["ETag"], validator)

    def test_it_vouches_for_one_reader_only(self):
        validator = self.client.get(self.url)["ETag"]
        self.client.force_login(
            get_user_model().objects.create_superuser("second", password="z" * 20)
        )
        mine, theirs = (RequestFactory().get(self.url) for _ in range(2))
        mine.user, theirs.user = self.user, get_user_model().objects.get(username="second")

        self.assertNotEqual(fragments.base(mine, "r"), fragments.base(theirs, "r"))
        again = self.client.get(self.url, headers={"If-None-Match": validator})
        self.assertEqual(again.status_code, 200)

    def test_a_validator_stops_holding_at_its_moment(self):
        request = RequestFactory().get(self.url, headers={"If-None-Match": f'"{"a" * 64}-1"'})

        self.assertIsNone(fragments.presented(request, "a" * 64))

    def test_an_unknown_input_is_never_unchanged(self):
        request = RequestFactory().get(self.url)
        request.user = self.user

        self.assertIsNone(fragments.base(request, None))
        self.assertIsNone(fragments.standing(None))
        self.assertIsNone(fragments.presented(request, None))
