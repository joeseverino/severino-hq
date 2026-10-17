"""The contact inbox: pages read the controller's stored reading, and a change
is queued for the controller. Nothing here reaches D1 or holds a credential."""

import ast
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, OperationRequest, ProviderConnection, ProviderInventory
from hq.platform.application.contact_submissions import (
    ContactDeleteCommand,
    ContactReviewCommand,
    execute_contact_delete,
    execute_contact_review,
    list_contact_submissions,
)
from hq.platform.application.security import Capability, Principal
from hq.platform.application.tests.source import parsed

from .. import inbox

REF = "example-cloudflare"
ACCOUNT = "a" * 32


def _row(identifier, *, database="uuid-1", name="Jane Doe", status="unread", **extra):
    return {
        "connection_ref": REF,
        "account_id": ACCOUNT,
        "database_id": database,
        "database": "contacts" if database == "uuid-1" else "other",
        "id": identifier,
        "created_at": "2026-07-18 12:00:00",
        "updated_at": "2026-07-18 12:00:00",
        "name": name,
        "email": "jane@example.com",
        "message": "Hello from the contact form.",
        "status": status,
        "country": "US",
        "turnstile": "verified",
        **extra,
    }


def _read(*records, reachable=True, error=""):
    ProviderInventory.objects.update_or_create(
        kind=inbox.READING,
        defaults={"records": list(records), "observed_at": timezone.now(), "reachable": reachable, "error": error},
    )


def _manages(manages=True):
    ProviderConnection.objects.update_or_create(
        connection_ref=REF,
        defaults={"provider": "cloudflare_api", "manages": manages, "observed_at": timezone.now()},
    )


def _principal(*capabilities):
    return Principal(actor="op", interface="web", capabilities=frozenset(capabilities))


OPERATOR = _principal(Capability.MANAGE_CONTACTS)


class InboxReadingTests(TestCase):
    def test_nothing_read_yet_is_calm_and_names_the_connection(self):
        self.client.force_login(get_user_model().objects.create_user("op"))

        response = self.client.get(reverse("contacts:list"))

        self.assertContains(response, "No messages read yet.")
        self.assertContains(response, "Connect Cloudflare API")
        self.assertNotContains(response, "msg-error")
        self.assertEqual(inbox.unread(), (0, "unknown"))

    def test_rows_counts_and_search_come_from_the_stored_reading(self):
        _read(
            _row(3, name="Example Person"),
            _row(2, name="Other Sender", status="read", message="about example"),
            _row(1, name="example two", status="spam"),
        )

        self.assertEqual(inbox.unread(), (1, "ok"))
        self.assertEqual([row["id"] for row in inbox.recent()], [3, 2, 1])
        self.assertEqual([row["id"] for row in inbox.search("EXAMPLE", limit=8)], [3, 1])
        self.assertEqual(
            [row["id"] for row in list_contact_submissions(query="example")["items"]],
            [3, 2, 1],
        )
        self.assertEqual([row["id"] for row in list_contact_submissions(status="read")["items"]], [2])

    def test_a_failed_read_keeps_the_rows_and_says_so(self):
        _read(_row(1), reachable=False, error="cloudflare refused the request")

        self.assertEqual(inbox.unread(), (1, "unavailable"))
        self.client.force_login(get_user_model().objects.create_user("op"))
        response = self.client.get(reverse("contacts:list"))
        self.assertContains(response, "cloudflare refused the request")
        self.assertContains(response, "Jane Doe")

    def test_several_databases_and_no_name_is_a_problem_naming_the_setting(self):
        _read(_row(1), _row(1, database="uuid-2"))

        current = inbox.current()

        self.assertEqual(current.state, inbox.UNAVAILABLE)
        self.assertIn("CLOUDFLARE_D1_DATABASE_NAME", current.problem)

    def test_the_named_database_is_chosen_among_several(self):
        _read(_row(1), _row(2, database="uuid-2"))

        with override_settings(CLOUDFLARE_D1_DATABASE_NAME="other"):
            current = inbox.current()

        self.assertEqual([row["id"] for row in current.rows], [2])
        self.assertEqual(current.database.database_id, "uuid-2")

    def test_the_generic_readings_resource_lists_no_message(self):
        from hq.platform.application.derived_reads import get_reading

        _read(_row(1))

        self.assertEqual(get_reading(inbox.READING)["items"], [])


class ContactViewTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("joe", password="pw"))
        _read(_row(1))

    def test_list_renders_with_tabs_and_actions(self):
        response = self.client.get(reverse("contacts:list"))
        self.assertContains(response, "Jane Doe")
        self.assertContains(response, "Hello from the contact form.")
        self.assertContains(response, "status-tab")
        self.assertContains(response, "Mark read")

    def test_detail_renders_quick_actions(self):
        response = self.client.get(reverse("contacts:detail", args=[1]))
        self.assertContains(response, "Mark replied")
        self.assertContains(response, "Delete")

    def test_detail_reply_link_cannot_add_headers(self):
        _read(_row(1, email="a@example.com\r\nBcc: b@example.com?cc=c@example.com"))
        body = self.client.get(reverse("contacts:detail", args=[1])).content.decode()
        self.assertIn(
            'href="mailto:a@example.comBcc%3A%20b@example.com%3Fcc%3Dc@example.com?subject=Re%3A%20your%20message"',
            body,
        )
        self.assertNotIn('href="mailto:a@example.com\r', body)

    def test_an_unknown_message_redirects_to_the_list(self):
        response = self.client.get(reverse("contacts:detail", args=[99]))
        self.assertRedirects(response, reverse("contacts:list"), fetch_redirect_response=False)

    @patch("hq.domains.contacts.views.execute_contact_review")
    def test_set_status_queues_and_redirects_back(self, review):
        response = self.client.post(
            reverse("contacts:set_status", args=[1]),
            {"status": "read", "next": reverse("contacts:list") + "?status=unread"},
        )
        self.assertEqual(review.call_args.args[0].status, "read")
        self.assertEqual(review.call_args.kwargs["current_id"], 1)
        self.assertRedirects(response, reverse("contacts:list") + "?status=unread", fetch_redirect_response=False)

    @patch("hq.domains.contacts.views.execute_contact_review")
    def test_set_status_rejects_unknown_status(self, review):
        self.client.post(reverse("contacts:set_status", args=[1]), {"status": "bogus"})
        review.assert_not_called()

    @patch("hq.domains.contacts.views.execute_contact_review")
    def test_set_status_ignores_offsite_next(self, review):
        response = self.client.post(
            reverse("contacts:set_status", args=[1]), {"status": "read", "next": "https://evil.example/"}
        )
        self.assertRedirects(response, reverse("contacts:list"), fetch_redirect_response=False)

    def test_delete_get_shows_confirm(self):
        response = self.client.get(reverse("contacts:delete", args=[1]))
        self.assertContains(response, "Delete message #1?")
        self.assertContains(response, f'formaction="{reverse("contacts:delete", args=[1])}"')

    @patch("hq.domains.contacts.views.execute_contact_delete")
    def test_delete_post_queues_and_redirects(self, delete):
        response = self.client.post(reverse("contacts:delete", args=[1]))
        self.assertEqual(delete.call_args.args[0].confirm, "1")
        self.assertRedirects(response, reverse("contacts:list"), fetch_redirect_response=False)

    def test_a_refused_change_is_said_and_queues_nothing(self):
        _manages(False)

        response = self.client.post(reverse("contacts:set_status", args=[1]), {"status": "read"}, follow=True)

        self.assertContains(response, "D1 Write (account)")
        self.assertFalse(ManagedResource.objects.filter(kind=inbox.DECLARATION).exists())
        self.assertFalse(OperationRequest.objects.exists())


class QueuedChangeTests(TestCase):
    def setUp(self):
        _read(_row(2), _row(1, status="read"))
        _manages()

    def test_a_review_is_queued_for_the_controller_and_shown_waiting(self):
        answer = execute_contact_review(
            ContactReviewCommand(status="replied", admin_notes="answered"), principal=OPERATOR, current_id=2
        )

        self.assertTrue(answer["queued"])
        resource = ManagedResource.objects.get(kind=inbox.DECLARATION)
        self.assertEqual(
            resource.spec["reviews"], [{"id": 2, "status": "replied", "assigned_to": "", "admin_notes": "answered"}]
        )
        self.assertEqual((resource.spec["connection_ref"], resource.spec["database_id"]), (REF, "uuid-1"))
        operation = OperationRequest.objects.get(resource=resource)
        self.assertEqual((operation.action, operation.state), ("reconcile", "queued"))
        row = inbox.current().rows[0]
        self.assertEqual((row["status"], row["pending"]), ("replied", True))
        self.assertEqual(inbox.current().waiting, 1)

    def test_a_change_the_reading_shows_leaves_the_declaration(self):
        execute_contact_review(ContactReviewCommand(status="replied"), principal=OPERATOR, current_id=2)
        _read(_row(2, status="replied"), _row(1, status="read"))
        self.assertFalse(inbox.current().rows[0]["pending"])

        execute_contact_review(ContactReviewCommand(status="spam"), principal=OPERATOR, current_id=1)

        resource = ManagedResource.objects.get(kind=inbox.DECLARATION)
        self.assertEqual([review["id"] for review in resource.spec["reviews"]], [1])
        self.assertEqual(resource.generation, 2)

    def test_a_deletion_hides_the_row_until_it_is_gone(self):
        execute_contact_delete(ContactDeleteCommand(confirm="2"), principal=OPERATOR, current_id=2)

        self.assertEqual([row["id"] for row in inbox.current().rows], [1])
        self.assertEqual(ManagedResource.objects.get(kind=inbox.DECLARATION).spec["deletions"], [2])
        again = execute_contact_delete(ContactDeleteCommand(confirm="2"), principal=OPERATOR, current_id=2)
        self.assertTrue(again["deleted"]["already_absent"])

    def test_the_controllers_refusal_is_shown(self):
        execute_contact_review(ContactReviewCommand(status="replied"), principal=OPERATOR, current_id=2)
        ManagedResource.objects.filter(kind=inbox.DECLARATION).update(
            generation=2,
            observed_generation=1,
            conditions=[{"type": "Degraded", "status": True, "message": "cloudflare refused the request"}],
        )

        self.assertEqual(inbox.current().refused, "cloudflare refused the request")

    def test_a_reviewer_needs_the_contacts_capability(self):
        from hq.platform.application.security import AuthorizationError

        with self.assertRaises(AuthorizationError):
            execute_contact_review(
                ContactReviewCommand(status="read"), principal=_principal(Capability.READ), current_id=2
            )
        self.assertFalse(ManagedResource.objects.exists())

    def test_invalid_input_is_refused(self):
        with self.assertRaisesMessage(ValueError, "Unknown contact status"):
            execute_contact_review(ContactReviewCommand(status="bogus"), principal=OPERATOR, current_id=2)
        with self.assertRaisesMessage(ValueError, "confirm must exactly match"):
            execute_contact_delete(ContactDeleteCommand(confirm="1"), principal=OPERATOR, current_id=2)
        with self.assertRaisesMessage(ValueError, "was not found"):
            execute_contact_review(ContactReviewCommand(status="read"), principal=OPERATOR, current_id=99)


class NoCredentialTests(TestCase):
    """The web process reads no provider credential for the inbox."""

    def test_no_cloudflare_token_setting_exists(self):
        self.assertFalse(hasattr(settings, "CLOUDFLARE_API_TOKEN"))

    def test_the_contacts_code_reads_no_token_setting(self):
        root = Path(settings.BASE_DIR)
        sources = [
            *sorted((root / "hq" / "domains" / "contacts").glob("*.py")),
            root / "hq" / "platform" / "application" / "contact_submissions.py",
        ]
        self.assertGreater(len(sources), 3)
        for path in sources:
            for node in ast.walk(parsed(path)):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertNotRegex(node.value, r"(?i)_(TOKEN|SECRET|PASSWORD|API_KEY)$", path.name)

    def test_no_connection_family_is_declared_for_contacts(self):
        from hq.platform.application.domains import host_domains

        contacts = next(domain for domain in host_domains() if domain.id == "hq.contacts")
        self.assertIsNone(contacts.integration.connections)


NOTE = "example private note"
ASSIGNEE = "example-assignee"


class WithheldSpecTests(TestCase):
    """A queued review's note and assignee are shown only under the contacts
    capability: every generic surface for records leaves the spec out."""

    def setUp(self):
        _read(_row(2), _row(1, status="read"))
        _manages()
        execute_contact_review(
            ContactReviewCommand(status="replied", assigned_to=ASSIGNEE, admin_notes=NOTE),
            principal=OPERATOR,
            current_id=2,
        )
        execute_contact_review(
            # The same message again: the spec changes where an audit diff would
            # show it, so the redaction is what keeps the note out.
            ContactReviewCommand(status="spam", assigned_to=ASSIGNEE, admin_notes=NOTE + " again"),
            principal=OPERATOR,
            current_id=2,
        )

    def assertWithheld(self, text):
        self.assertNotIn(NOTE, text)
        self.assertNotIn(ASSIGNEE, text)

    def test_the_resources_api_with_read_shows_no_spec(self):
        from hq.platform.api.testing import ContractClient
        from hq.platform.api.tests.test_api import ISSUER, RESOURCE, _serving, _token

        client = ContractClient()
        with override_settings(OIDC_ISSUER=ISSUER, SEVERINO_API_RESOURCE=RESOURCE), _serving():
            token = _token(scope="read")
            listing = client.get("/api/v2/resources/infrastructure.resources/", HTTP_AUTHORIZATION=f"Bearer {token}")
            detail = client.get(
                f"/api/v2/resources/infrastructure.resources/{inbox.DECLARATION_KEY}/",
                HTTP_AUTHORIZATION=f"Bearer {token}",
            )
            contacts = client.get("/api/v2/resources/contact.submissions/", HTTP_AUTHORIZATION=f"Bearer {token}")

        self.assertEqual((listing.status_code, detail.status_code), (200, 200))
        self.assertIn(inbox.DECLARATION_KEY, listing.content.decode())
        self.assertWithheld(listing.content.decode())
        self.assertWithheld(detail.content.decode())
        self.assertNotEqual(contacts.status_code, 200)

    def test_mcp_shows_no_spec(self):
        from hq.platform.application.security import mcp_principal
        from hq.platform.mcp import services

        with patch.object(services, "current_principal", mcp_principal):
            listing = services.list_resource("infrastructure.resources")
            detail = services.get_resource("infrastructure.resources", inbox.DECLARATION_KEY)

        self.assertWithheld(str(listing))
        self.assertWithheld(str(detail))
        self.assertEqual(detail["resource"]["spec"], {})

    def test_the_record_pages_report_and_form_show_no_spec(self):
        self.client.force_login(get_user_model().objects.create_user("op"))
        key = inbox.DECLARATION_KEY

        detail = self.client.get(reverse("control_plane:detail", args=[key]), follow=True)
        listing = self.client.get(reverse("control_plane:list"))
        report = self.client.get(reverse("control_plane:report_download", args=[key]))
        form = self.client.get(reverse("control_plane:edit", args=[key]))

        for response in (detail, listing, report):
            self.assertEqual(response.status_code, 200)
            self.assertWithheld(response.content.decode())
        self.assertEqual(form.status_code, 404)

    def test_search_does_not_index_the_spec(self):
        from hq.platform.search_index.models import SearchDocument

        bodies = " ".join(SearchDocument.objects.values_list("body", flat=True))
        self.assertIn(inbox.DECLARATION_KEY, " ".join(SearchDocument.objects.values_list("object_id", flat=True)))
        self.assertWithheld(bodies)

    def test_the_audit_log_says_what_changed_without_the_note(self):
        from hq.platform.core.models import AuditLog

        rows = list(AuditLog.objects.values_list("message", "metadata"))
        self.assertTrue(any("spec" in str(metadata) for _, metadata in rows), rows)
        self.assertWithheld(str(rows))

    def test_the_generic_writer_refuses_the_kind(self):
        from hq.platform.application.infrastructure import ManagedResourceCommand, PolicyError, save_managed_resource

        with self.assertRaises(PolicyError):
            save_managed_resource(
                ManagedResourceCommand(key=inbox.DECLARATION_KEY, kind=inbox.DECLARATION, spec={}),
                principal=_principal(Capability.MANAGE_INFRASTRUCTURE),
                current_key=inbox.DECLARATION_KEY,
            )

    def test_the_contacts_capability_sees_the_pending_review(self):
        from hq.platform.application.resources import get_resource
        from hq.platform.application.security import AuthorizationError

        found = get_resource("contact.submissions", 2, principal=OPERATOR)

        self.assertEqual(
            (found["admin_notes"], found["assigned_to"], found["pending"]), (NOTE + " again", ASSIGNEE, True)
        )
        with self.assertRaises(AuthorizationError):
            get_resource("contact.submissions", 2, principal=_principal(Capability.READ))
