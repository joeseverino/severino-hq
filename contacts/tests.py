"""Contact review screens, with the D1 bridge mocked out."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from . import d1
from .d1 import get_dashboard_state

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

    @patch(
        "contacts.views.get_submission",
        return_value={**SUBMISSION, "email": "a@example.com\r\nBcc: b@example.com?cc=c@example.com"},
    )
    def test_detail_reply_link_cannot_add_headers(self, mock_get):
        response = self.client.get(reverse("contacts:detail", args=[1]))
        body = response.content.decode()
        self.assertIn(
            'href="mailto:a@example.comBcc%3A%20b@example.com%3Fcc%3Dc@example.com'
            '?subject=Re%3A%20your%20message"',
            body,
        )
        self.assertNotIn('href="mailto:a@example.com\r', body)

    @patch("contacts.views.execute_contact_review")
    @patch("contacts.views.get_submission", return_value=dict(SUBMISSION))
    def test_set_status_flips_and_redirects_back(self, mock_get, mock_review):
        response = self.client.post(
            reverse("contacts:set_status", args=[1]),
            {"status": "read", "next": reverse("contacts:list") + "?status=unread"},
        )
        self.assertEqual(mock_review.call_args.args[0].status, "read")
        self.assertEqual(mock_review.call_args.kwargs["current_id"], 1)
        self.assertRedirects(
            response,
            reverse("contacts:list") + "?status=unread",
            fetch_redirect_response=False,
        )

    @patch("contacts.views.execute_contact_review")
    @patch("contacts.views.get_submission", return_value=dict(SUBMISSION))
    def test_set_status_rejects_unknown_status(self, mock_get, mock_review):
        self.client.post(
            reverse("contacts:set_status", args=[1]), {"status": "bogus"}
        )
        mock_review.assert_not_called()

    @patch("contacts.views.execute_contact_review")
    @patch("contacts.views.get_submission", return_value=dict(SUBMISSION))
    def test_set_status_ignores_offsite_next(self, mock_get, mock_review):
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
        self.assertContains(response, f'formaction="{reverse("contacts:delete", args=[1])}"')

    @patch("contacts.views.execute_contact_delete")
    @patch("contacts.views.get_submission", return_value=dict(SUBMISSION))
    def test_delete_post_deletes_and_redirects(self, mock_get, mock_delete):
        response = self.client.post(reverse("contacts:delete", args=[1]))
        self.assertEqual(mock_delete.call_args.args[0].confirm, "1")
        self.assertEqual(mock_delete.call_args.kwargs["current_id"], 1)
        self.assertRedirects(
            response, reverse("contacts:list"), fetch_redirect_response=False
        )


class ContactProjectionTests(TestCase):
    @patch("contacts.d1.query")
    def test_dashboard_rows_and_unread_total_share_one_query(self, query):
        query.return_value = [
            {
                "id": 4,
                "name": "Jane",
                "status": "unread",
                "created_at": "2026-08-23",
                "email": "jane@example.com",
                "country": "US",
                "unread_count": 9,
            }
        ]

        rows, unread = get_dashboard_state(limit=4)

        query.assert_called_once()
        self.assertEqual(unread, 9)
        self.assertNotIn("unread_count", rows[0])

    def test_the_stored_dashboard_rows_select_no_email_or_message(self):
        with patch.object(d1, "query", return_value=[]) as query:
            get_dashboard_state(limit=4)
        sql = query.call_args.args[0]
        self.assertNotIn("email", sql)
        self.assertNotIn("message", sql)


class InboxTests(TestCase):
    """The unread count is stored; only requests made after a page read D1."""

    def test_a_fresh_count_is_not_read_again(self):
        from . import inbox

        with patch("contacts.d1.get_dashboard_state", return_value=([], 3)):
            inbox.refresh()
        with patch("contacts.d1.get_dashboard_state", side_effect=AssertionError("read again")):
            inbox.refresh()

        self.assertEqual(inbox.unread(), (3, "ok"))

    def test_a_write_reads_the_stored_state_again(self):
        from . import inbox

        with patch("contacts.d1.get_dashboard_state", return_value=([], 3)):
            inbox.refresh()
        with (
            patch("contacts.d1.query", return_value=[]),
            patch("contacts.d1.get_dashboard_state", return_value=([], 2)) as read,
        ):
            d1.set_status(1, "read")

        read.assert_called_once()
        self.assertEqual(inbox.unread(), (2, "ok"))

    def test_an_outage_keeps_the_last_count_and_says_so(self):
        from application import readings

        from . import inbox

        readings.record(d1.UNREAD, {"count": 4, "status": "ok"})
        readings.expire(d1.UNREAD)
        with patch("contacts.d1.get_dashboard_state", side_effect=d1.D1Error("down")):
            inbox.refresh()

        self.assertEqual(inbox.unread(), (4, "unavailable"))

    def test_the_rows_come_from_the_same_read_and_survive_an_outage(self):
        from application import readings

        from . import inbox

        row = {"id": 7, "name": "Jane", "status": "unread", "created_at": "2026-08-23"}
        with patch("contacts.d1.get_dashboard_state", return_value=([row], 1)) as read:
            inbox.refresh()
        read.assert_called_once_with(limit=inbox.KEPT_LIMIT)
        self.assertEqual((inbox.recent(), inbox.unread()), ([row], (1, "ok")))

        readings.expire(d1.UNREAD)
        with patch("contacts.d1.get_dashboard_state", side_effect=d1.D1Error("down")):
            inbox.refresh()
        self.assertEqual((inbox.recent(), inbox.unread()), ([row], (1, "unavailable")))

    def test_the_header_count_request_does_not_read_d1(self):
        from django.contrib.auth import get_user_model

        self.client.force_login(get_user_model().objects.create_user("op", password="x" * 20))
        with patch("contacts.d1.query", side_effect=AssertionError("a GET called D1")):
            response = self.client.get(reverse("action_item_count"))

        self.assertEqual(response.status_code, 200)

    def test_the_timer_command_reads_d1_even_when_fresh(self):
        from io import StringIO

        from django.core.management import call_command

        from . import inbox

        with patch("contacts.d1.get_dashboard_state", return_value=([], 3)):
            inbox.refresh()
        out = StringIO()
        with patch("contacts.d1.get_dashboard_state", return_value=([], 5)) as read:
            call_command("refresh_contacts_inbox", stdout=out)

        read.assert_called_once_with(limit=inbox.KEPT_LIMIT)
        self.assertEqual(inbox.unread(), (5, "ok"))
        self.assertIn('"unread": 5', out.getvalue())

    def test_search_matches_stored_names_without_reading_d1(self):
        from . import inbox

        rows = [
            {"id": 9, "name": "Example Person", "status": "unread", "created_at": "2026-09-02"},
            {"id": 8, "name": "Other Sender", "status": "read", "created_at": "2026-09-01"},
            {"id": 7, "name": "example two", "status": "read", "created_at": "2026-08-30"},
        ]
        with patch("contacts.d1.get_dashboard_state", return_value=(rows, 1)):
            inbox.refresh()
        with patch("contacts.d1.query", side_effect=AssertionError("search called D1")):
            found = inbox.search("EXAMPLE", limit=8)
            capped = inbox.search("example", limit=1)

        self.assertEqual([row["id"] for row in found], [9, 7])
        self.assertEqual([row["id"] for row in capped], [9])
        self.assertEqual(inbox.recent(), rows)

    def test_the_search_page_shows_stored_contacts_without_reading_d1(self):
        from django.contrib.auth import get_user_model

        from application import readings

        self.client.force_login(get_user_model().objects.create_user("op", password="x" * 20))
        row = {"id": 9, "name": "Example Person", "status": "unread", "created_at": "2026-09-02"}
        readings.record(d1.UNREAD, {"count": 1, "rows": [row], "status": "ok"})
        with patch("contacts.d1.query", side_effect=AssertionError("search called D1")):
            response = self.client.get(reverse("search"), {"q": "example person"})

        self.assertEqual(response.context["contacts"], [row])
        self.assertContains(response, reverse("contacts:detail", args=[9]))


def _databases(*records):
    from django.utils import timezone

    from control_plane.models import ProviderInventory

    ProviderInventory.objects.update_or_create(
        kind="cloudflare.d1_database",
        defaults={"records": list(records), "observed_at": timezone.now()},
    )


def _db(name, uuid, account="a" * 32):
    return {"account_id": account, "name": name, "uuid": uuid}


@override_settings(
    CLOUDFLARE_ACCOUNT_ID="",
    CLOUDFLARE_D1_DATABASE_ID="",
    CLOUDFLARE_D1_DATABASE_NAME="",
    CLOUDFLARE_API_TOKEN="",
)
class D1DerivationTests(TestCase):
    def test_settings_win_over_the_reading(self):
        _databases(_db("contacts", "uuid-1"))
        with override_settings(
            CLOUDFLARE_ACCOUNT_ID="b" * 32, CLOUDFLARE_D1_DATABASE_ID="uuid-set"
        ):
            target = d1.database()

        self.assertEqual((target.account, target.database), ("b" * 32, "uuid-set"))
        self.assertEqual(target.source, "settings")

    def test_the_only_database_is_derived(self):
        _databases(_db("contacts", "uuid-1"))

        target = d1.database()

        self.assertEqual((target.account, target.database), ("a" * 32, "uuid-1"))
        self.assertIn("cloudflare.d1_database", target.source)
        self.assertIn("/accounts/" + "a" * 32 + "/d1/database/uuid-1/", d1._endpoint())

    def test_the_named_database_is_chosen_among_several(self):
        _databases(_db("contacts", "uuid-1"), _db("other", "uuid-2"))

        with override_settings(CLOUDFLARE_D1_DATABASE_NAME="other"):
            self.assertEqual(d1.database().database, "uuid-2")

    def test_a_set_database_id_derives_only_the_account(self):
        _databases(_db("contacts", "uuid-1"), _db("other", "uuid-2", account="c" * 32))

        with override_settings(CLOUDFLARE_D1_DATABASE_ID="uuid-2"):
            target = d1.database()

        self.assertEqual((target.account, target.database), ("c" * 32, "uuid-2"))

    def test_several_databases_and_no_name_is_an_error_naming_the_setting(self):
        _databases(_db("contacts", "uuid-1"), _db("other", "uuid-2"))

        with self.assertRaisesMessage(d1.D1Error, "CLOUDFLARE_D1_DATABASE_NAME"):
            d1.database()

    def test_no_reading_is_an_error_naming_the_settings(self):
        with self.assertRaisesMessage(d1.D1Error, "CLOUDFLARE_D1_DATABASE_ID"):
            d1.database()

    def test_a_name_that_matches_nothing_is_an_error(self):
        _databases(_db("contacts", "uuid-1"))

        with override_settings(CLOUDFLARE_D1_DATABASE_NAME="missing"):
            with self.assertRaisesMessage(d1.D1Error, "'missing'"):
                d1.database()

    def test_an_underivable_database_shows_on_the_connection(self):
        _databases(_db("contacts", "uuid-1"), _db("other", "uuid-2"))

        with override_settings(CLOUDFLARE_API_TOKEN="token"):
            (spec,) = d1.connection_specs()
            (instance,) = spec.instance_provider()

        self.assertEqual(instance.status, "attention")
        self.assertIn("CLOUDFLARE_D1_DATABASE_NAME", instance.detail)
