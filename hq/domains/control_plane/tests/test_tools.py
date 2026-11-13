"""The tools page: a typed lookup is a GET, re-reading a stored answer a POST."""

from datetime import UTC, datetime
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.platform.application.plugin_testing import form_keyed
from hq.platform.application.tests.markup import select

ADDRESS = "203.0.113.7"


class ToolsRefreshTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_superuser("operator", password="x" * 20)
        self.client.force_login(user)

    def test_a_get_never_asks_for_a_refresh(self):
        with mock.patch("hq.domains.control_plane.tool_views.execute_posted", return_value={"ok": True}) as execute:
            self.client.get(
                reverse("control_plane:tools"),
                {"tab": "dns", "address": ADDRESS, "refresh": "1"},
            )

        payloads = [call.args[2] for call in execute.call_args_list]
        self.assertTrue(payloads)
        self.assertFalse(any(payload.get("refresh") for payload in payloads))

    def test_a_post_refreshes_and_returns_to_the_result(self):
        with mock.patch("hq.domains.control_plane.tool_views.execute_posted", return_value={"ok": True}) as execute:
            response = self.client.post(reverse("control_plane:tools"), form_keyed({"tab": "dns", "address": ADDRESS}))

        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[2], {"address": ADDRESS, "refresh": True})
        self.assertRedirects(
            response,
            reverse("control_plane:tools") + f"?tab=dns&address={ADDRESS}",
            fetch_redirect_response=False,
        )

    def test_a_refused_refresh_says_why_on_the_page_it_returns_to(self):
        refused = {"ok": False, "error": {"code": "forbidden", "message": "An example refusal."}}
        with mock.patch("hq.domains.control_plane.tool_views.execute_posted", return_value=refused):
            response = self.client.post(
                reverse("control_plane:tools"), form_keyed({"tab": "dns", "address": ADDRESS}), follow=True
            )

        self.assertIn("An example refusal.", [str(message) for message in response.context["messages"]])


def _sent(response, form):
    """What ``form`` sends: each named input inside it, by name."""

    return {
        element.attrs["name"]: element.attrs.get("value", "")
        for element in select(response, "name")
        if element.tag == "input" and form in element.ancestors
    }


class ToolsFormTests(TestCase):
    """Each lookup is the shared form, asked by a GET whose fields are the query its link builds."""

    def setUp(self):
        user = get_user_model().objects.create_superuser("operator", password="x" * 20)
        self.client.force_login(user)

    def _page(self, query, result):
        with mock.patch("hq.domains.control_plane.tool_views.execute_posted", return_value=result):
            return self.client.get(reverse("control_plane:tools"), query)

    def test_each_lookup_asks_by_get_with_what_was_asked(self):
        response = self._page({"tab": "dns", "name": "example.test", "address": ADDRESS}, {"ok": False})

        for field, value in (("name", "example.test"), ("address", ADDRESS)):
            with self.subTest(field=field):
                (control,) = select(response, "name", field)
                (form,) = (ancestor for ancestor in control.ancestors if ancestor.tag == "form")
                self.assertEqual(form.attrs["method"], "get")
                self.assertEqual(_sent(response, form), {"tab": "dns", field: value})
                (button,) = (element for element in select(response, "type", "submit") if form in element.ancestors)
                self.assertTrue(any("form-actions" in ancestor.classes for ancestor in button.ancestors))

    def test_a_stored_answer_is_read_again_by_a_post_of_the_address(self):
        stored = {
            "ok": True,
            "address": ADDRESS,
            "version": 4,
            "observed_at": datetime(2026, 1, 2, tzinfo=UTC).isoformat(),
        }
        response = self._page({"tab": "dns", "address": ADDRESS}, stored)

        (form,) = (form for form in select(response, "method", "post") if "address" in _sent(response, form))
        sent = _sent(response, form)
        self.assertIn(form.attrs.get("action", ""), ("", reverse("control_plane:tools")))
        self.assertEqual((sent["tab"], sent["address"]), ("dns", ADDRESS))
