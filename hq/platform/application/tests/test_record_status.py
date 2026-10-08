"""A record has one name, one status line, and a history of what changed."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.domains.control_plane.provider_adapters.github import COMPOSE_WORKFLOW, CURRENT, KIND

from ..entity_links import entity_link, record_name
from ..inventory import confirm_observed
from ..projection import projection_scope
from ..resource_context import record_status
from ..resource_operations import changes, operation_summary

READY = [{"type": "Ready", "status": True, "reason": "Observed", "message": "Matches HQ's settings."}]


def rewrite(key: str = "app-dns", **fields) -> ManagedResource:
    return ManagedResource.objects.create(
        key=key,
        kind="adguard.rewrite",
        spec={"domain": "app.example.com", "answer": "192.0.2.10"},
        **{"generation": 1, "observed_generation": 1, **fields},
    )


class RecordNameTests(TestCase):
    def test_a_record_is_named_by_what_it_stands_for(self):
        for kind, spec, name in (
            ("adguard.rewrite", {"domain": "app.example.com", "answer": "192.0.2.10"}, "app.example.com → 192.0.2.10"),
            (
                "npm.proxy_host",
                {"domain_names": ["app.example.com"], "forward_host": "127.0.0.1", "forward_port": 8000},
                "app.example.com → 127.0.0.1:8000",
            ),
            (
                "cloudflare.dns_record",
                {"zone": "example.com", "name": "www.example.com", "record_type": "A", "content": "203.0.113.10"},
                "www.example.com → 203.0.113.10",
            ),
            (
                "cloudflare.dns_record",
                {"zone": "example.com", "name": "example.com", "record_type": "TXT", "content": "v=spf1 -all"},
                "example.com TXT",
            ),
            ("cloudflare.zone", {"zone": "example.com"}, "example.com"),
            ("portainer.container", {"host": "lab-1", "name": "web"}, "web"),
            # A certificate covers many names and is known by its own.
            ("tls.certificate", {"certificate_name": "example", "domains": ["*.example.com"]}, "the-key"),
        ):
            with self.subTest(kind=kind):
                self.assertEqual(record_name(kind, spec, "the-key"), name)

    def test_a_spec_that_cannot_be_read_keeps_its_key(self):
        self.assertEqual(record_name("npm.proxy_host", {"domain_names": 7}, "the-key"), "the-key")
        self.assertEqual(record_name("unknown.kind", {}, "the-key"), "the-key")

    def test_the_link_builder_shows_the_name_and_keeps_the_key(self):
        rewrite()

        with projection_scope(), self.assertNumQueries(1):
            first = entity_link("resource", "app-dns")
            again = entity_link("resource", "app-dns")

        self.assertEqual(first.label, "app.example.com → 192.0.2.10")
        self.assertEqual((first.identity, first.title), ("app-dns", "app-dns"))
        self.assertEqual(first.url, reverse("control_plane:detail", args=["app-dns"]))
        self.assertEqual(again, first)


class RecordPageTests(TestCase):
    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        rewrite(conditions=READY, last_observed_at=timezone.now())
        user = get_user_model().objects.create_user("operator", password="pw", is_staff=True, is_superuser=True)
        self.client.force_login(user)

    def test_the_page_is_titled_by_the_name_and_says_each_setting_once(self):
        response = self.client.get(reverse("control_plane:detail", args=["app-dns"]))

        self.assertContains(response, "<title>app.example.com → 192.0.2.10")
        # Its two settings are its name and its readout, so nothing is left to repeat.
        self.assertNotContains(response, "Other settings")
        self.assertContains(response, 'data-record-status="working"')
        self.assertContains(response, "Matches HQ&#x27;s settings.")

    def test_the_list_names_it_and_gives_one_state(self):
        response = self.client.get(reverse("control_plane:list"))
        row = response.content.decode().split('title="app-dns">')[1].split("</tr>")[0]

        self.assertTrue(row.startswith("app.example.com → 192.0.2.10</a>"))
        self.assertEqual(row.count('class="pill'), 1)
        self.assertIn("Working", row)

    def test_a_row_about_its_own_service_shows_where_the_record_leads(self):
        from ..service_declarations import Claim

        with projection_scope():
            link = Claim("app-dns", "adguard.rewrite", {}).within_service

        self.assertEqual((link.label, link.identity), ("192.0.2.10", "app-dns"))


class RecordStatusTests(TestCase):
    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        self.now = timezone.now()

    def test_a_record_found_as_set_is_working(self):
        status = record_status(rewrite(conditions=READY, last_observed_at=self.now))

        self.assertEqual((status.state, status.label), ("working", "Working"))
        self.assertEqual(status.detail, "Matches HQ's settings.")

    def test_a_record_not_read_with_the_rest_of_its_type_is_never_working(self):
        stale = rewrite(conditions=READY, last_observed_at=self.now - timedelta(days=14))

        status = record_status(stale, newest=self.now)

        self.assertEqual((status.state, status.label), ("unseen", "Not found lately"))

    def test_a_record_switched_off_says_only_that(self):
        off = rewrite(conditions=READY, enabled=False, last_observed_at=self.now - timedelta(days=14))

        self.assertEqual(record_status(off, newest=self.now).label, "Switched off in HQ")

    def test_a_change_made_outside_hq_says_since_when_and_the_two_ways_out(self):
        since = (self.now - timedelta(minutes=4)).isoformat()
        drifted = rewrite(
            conditions=[
                {
                    "type": "Drifted",
                    "status": True,
                    "reason": "Drifted",
                    "since": since,
                    "message": "Points at is now 192.0.2.99. HQ set it to 192.0.2.10.",
                }
            ]
        )

        status = record_status(drifted)

        self.assertEqual((status.state, status.label), ("drifted", "Changed outside HQ"))
        self.assertEqual(status.since.isoformat(), since)
        self.assertTrue(status.detail.endswith("Keep the live version, or restore HQ's version."))

    def test_a_change_not_yet_applied_is_waiting(self):
        status = record_status(rewrite(generation=2, conditions=READY))

        self.assertEqual((status.state, status.label), ("pending", "Change waiting to apply"))


class ReportedFieldTests(TestCase):
    """A field holding the thing's own report is a problem in its words, never drift."""

    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        self.spec = {
            "repository": "example/host",
            "workflow": COMPOSE_WORKFLOW,
            "branch": "main",
            "production": CURRENT,
        }
        self.resource = ManagedResource.objects.create(
            key="delivery",
            kind=KIND,
            spec=self.spec,
            generation=1,
            observed_generation=1,
            last_observed_at=timezone.now(),
            status={**self.spec, "extensions": []},
        )
        self.behind = (
            "example.alpha: bbbbbbb is approved, production still runs aaaaaaa. "
            "Deploy run 9 finished without deploying it."
        )
        user = get_user_model().objects.create_user("operator", password="pw", is_staff=True, is_superuser=True)
        self.client.force_login(user)

    def read(self, production: str) -> None:
        confirm_observed({KIND: {"ok": True, "records": [{**self.spec, "production": production, "extensions": []}]}})
        self.resource.refresh_from_db()

    def test_what_it_reports_is_the_problem_in_its_own_words(self):
        self.read(self.behind)

        (condition,) = self.resource.conditions
        self.assertEqual((condition["type"], condition["reason"]), ("Degraded", "Reported"))
        self.assertEqual(condition["message"], self.behind)

    def test_the_page_shows_one_status_and_nothing_that_cannot_deploy(self):
        self.read(self.behind)

        response = self.client.get(reverse("control_plane:detail", args=["delivery"]))
        page = response.content.decode()

        self.assertEqual(page.count("data-record-status="), 1)
        self.assertContains(response, 'data-record-status="degraded"')
        self.assertContains(response, "Deploy run 9 finished without deploying it.")
        for dead_end in (
            "Keep the live version",
            "Restore HQ's version",
            "In sync",
            "Drift detected",
            "Changed outside HQ",
        ):
            self.assertNotContains(response, dead_end)
        self.assertContains(response, "Open the deploy on GitHub")
        self.assertContains(response, "https://github.com/example/host/actions")

    def test_a_problem_it_reports_offers_no_way_to_keep_or_restore_a_side(self):
        from ..findings import estate_findings
        from ..security import Capability, Principal

        self.read(self.behind)
        manager = Principal("operator", "test", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))

        with projection_scope():
            (finding,) = [item for item in estate_findings(principal=manager) if item.subject == "resource:delivery"]

        self.assertEqual(finding.remedies, ())
        self.assertEqual(finding.no_help_reason, "HQ cannot fix what HQ deploys reports.")
        self.assertEqual(finding.rule, "reconciled-but-still-wrong")
        self.assertEqual(finding.title, "example.alpha: bbbbbbb is approved, production still runs aaaaaaa")

    def test_a_current_production_matches(self):
        self.read(CURRENT)

        (condition,) = self.resource.conditions
        self.assertEqual((condition["type"], condition["message"]), ("Ready", "Matches HQ's settings."))


class HistoryShowsChangesTests(TestCase):
    def setUp(self):
        self.resource = rewrite()
        self.at = timezone.now() - timedelta(hours=1)
        self.count = 0

    def ran(
        self,
        message: str,
        *,
        condition: str = "Ready",
        reason: str = "Reconciled",
        by: str = "controller",
        state: str = OperationRequest.State.SUCCEEDED,
    ) -> OperationRequest:
        self.count += 1
        operation = OperationRequest.objects.create(
            resource=self.resource,
            action=OperationRequest.Action.RECONCILE,
            state=state,
            requested_actor="example-controller" if by == "controller" else "operator",
            requested_interface=by,
            idempotency_key=f"test:{self.count}",
            completed_at=self.at + timedelta(minutes=self.count),
            result={
                "message": message,
                "conditions": [{"type": condition, "status": True, "reason": reason, "message": message}],
            },
        )
        OperationRequest.objects.filter(pk=operation.pk).update(created_at=self.at + timedelta(minutes=self.count))
        return operation

    def listed(self, limit: int = 20) -> list[str]:
        return [
            operation_summary(operation)["headline"] for operation in changes(self.resource.operations.all(), limit)
        ]

    def test_a_check_that_keeps_finding_the_same_thing_is_one_row(self):
        for _minute in range(30):
            self.ran("Unchanged.")
        self.ran("Not deployed.", condition="Degraded", reason="NotDelivered")
        for _minute in range(30):
            self.ran("Unchanged.")

        self.assertEqual(self.listed(), ["Unchanged.", "Not deployed.", "Unchanged."])

    def test_what_a_person_asked_for_is_always_listed(self):
        self.ran("Unchanged.")
        self.ran("Unchanged.", by="web")
        self.ran("Unchanged.", by="web")

        self.assertEqual(len(self.listed()), 3)

    def test_a_failure_is_always_listed(self):
        self.ran("It was refused.", state=OperationRequest.State.FAILED)
        self.ran("It was refused.", state=OperationRequest.State.FAILED)

        self.assertEqual(len(self.listed()), 2)

    def test_a_run_that_found_a_problem_never_reads_as_done(self):
        summary = operation_summary(
            self.ran(
                "Deploy reported.",
                condition="Degraded",
                reason="NotDelivered",
            )
        )

        self.assertEqual((summary["tone"], summary["state_label"]), ("attention", "Problem found"))
        self.assertNotIn("guidance", summary)
        self.assertNotIn("No action is required", str(summary))

    def test_who_asked_reads_as_a_person_would_say_it(self):
        self.assertEqual(operation_summary(self.ran("Unchanged."))["by"], "Automatic")
        self.assertEqual(operation_summary(self.ran("Unchanged.", by="web"))["by"], "operator")
        self.assertEqual(operation_summary(self.ran("Unchanged.", by="mcp"))["by"], "operator (agent)")
