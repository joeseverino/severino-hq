"""The tools page: a typed lookup is a GET, re-reading a stored answer a POST."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

ADDRESS = "203.0.113.7"


class ToolsRefreshTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_superuser("operator", password="x" * 20)
        self.client.force_login(user)

    def test_a_get_never_asks_for_a_refresh(self):
        with mock.patch(
            "application.capabilities.execute_capability", return_value={"ok": True}
        ) as execute:
            self.client.get(
                reverse("control_plane:tools"),
                {"tab": "dns", "address": ADDRESS, "refresh": "1"},
            )

        payloads = [call.args[1] for call in execute.call_args_list]
        self.assertTrue(payloads)
        self.assertFalse(any(payload.get("refresh") for payload in payloads))

    def test_a_post_refreshes_and_returns_to_the_result(self):
        with mock.patch(
            "application.capabilities.execute_capability", return_value={"ok": True}
        ) as execute:
            response = self.client.post(
                reverse("control_plane:tools"), {"tab": "dns", "address": ADDRESS}
            )

        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[1], {"address": ADDRESS, "refresh": True})
        self.assertRedirects(
            response,
            reverse("control_plane:tools") + f"?tab=dns&address={ADDRESS}",
            fetch_redirect_response=False,
        )
