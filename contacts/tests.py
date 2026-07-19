"""Contact review screens, with the D1 bridge mocked out."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

SUBMISSION = {
    "id": 1,
    "created_at": "2026-07-18 12:00:00",
    "updated_at": "2026-07-18 12:00:00",
    "name": "Jane Doe",
    "email": "jane@example.com",
    "message": "Hello from the contact form.",
    "message_preview": "Hello from the contact form.",
    "status": "unread",
    "country": "US",
    "turnstile": "verified",
    "ip_address": None,
    "user_agent": None,
    "browser": None,
    "device": None,
    "source_url": None,
    "assigned_to": None,
    "admin_notes": None,
}


class ContactViewTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("joe", password="pw")
        self.client.force_login(user)

    @patch("contacts.views.status_counts", return_value={"unread": 1})
    @patch("contacts.views.list_submissions", return_value=[SUBMISSION])
    def test_list_renders_with_tabs_and_actions(self, mock_list, mock_counts):
        response = self.client.get(reverse("contacts:list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Jane Doe")
        self.assertContains(response, "Hello from the contact form.")
        self.assertContains(response, "status-tab")
        self.assertContains(response, "Mark read")

    @patch("contacts.views.status_counts", return_value={"unread": 1})
    @patch("contacts.views.list_submissions", return_value=[SUBMISSION])
    def test_list_passes_filters_to_d1(self, mock_list, mock_counts):
        self.client.get(reverse("contacts:list"), {"status": "unread", "q": "jane"})
        mock_list.assert_called_once_with(status="unread", q="jane")

    @patch("contacts.views.get_submission", return_value=dict(SUBMISSION))
    def test_detail_renders_quick_actions(self, mock_get):
        response = self.client.get(reverse("contacts:detail", args=[1]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Mark replied")
        self.assertContains(response, "Delete")

    @patch("contacts.views.set_status")
    @patch("contacts.views.get_submission", return_value=dict(SUBMISSION))
    def test_set_status_flips_and_redirects_back(self, mock_get, mock_set):
        response = self.client.post(
            reverse("contacts:set_status", args=[1]),
            {"status": "read", "next": reverse("contacts:list") + "?status=unread"},
        )
        mock_set.assert_called_once_with(1, "read")
        self.assertRedirects(
            response,
            reverse("contacts:list") + "?status=unread",
            fetch_redirect_response=False,
        )

    @patch("contacts.views.set_status")
    @patch("contacts.views.get_submission", return_value=dict(SUBMISSION))
    def test_set_status_rejects_unknown_status(self, mock_get, mock_set):
        self.client.post(
            reverse("contacts:set_status", args=[1]), {"status": "bogus"}
        )
        mock_set.assert_not_called()

    @patch("contacts.views.set_status")
    @patch("contacts.views.get_submission", return_value=dict(SUBMISSION))
    def test_set_status_ignores_offsite_next(self, mock_get, mock_set):
        response = self.client.post(
            reverse("contacts:set_status", args=[1]),
            {"status": "read", "next": "https://evil.example/"},
        )
        self.assertRedirects(
            response, reverse("contacts:list"), fetch_redirect_response=False
        )

    @patch("contacts.views.get_submission", return_value=dict(SUBMISSION))
    def test_delete_get_shows_confirm(self, mock_get):
        response = self.client.get(reverse("contacts:delete", args=[1]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Delete submission #1?")

    @patch("contacts.views.delete_submission")
    @patch("contacts.views.get_submission", return_value=dict(SUBMISSION))
    def test_delete_post_deletes_and_redirects(self, mock_get, mock_delete):
        response = self.client.post(reverse("contacts:delete", args=[1]))
        mock_delete.assert_called_once_with(1)
        self.assertRedirects(
            response, reverse("contacts:list"), fetch_redirect_response=False
        )
