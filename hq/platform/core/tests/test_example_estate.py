"""The example estate reads like an HQ that has run for months: every page the
navigation offers has something on it, and its history is spread over that
time by the people and machines that made it."""

from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.analytics.models import RumDaily
from hq.domains.assets.models import Asset
from hq.domains.contacts import inbox
from hq.domains.content.models import PAGE_TYPES, WRITEUP_TYPES, ContentItem
from hq.domains.expenses.models import Expense
from hq.domains.jobs import runner
from hq.domains.jobs.models import Job
from hq.domains.projects.models import Project
from hq.platform.application import agent_registry, analytics, approvals, github_profile, own_devices
from hq.platform.application.capability_policy import RULE_LABELS
from hq.platform.application.findings import findings
from hq.platform.application.linked_accounts import GITHUB, linked_login
from hq.platform.application.security import WEB_INTERFACE, cli_principal, web_principal
from hq.platform.application.tests.markup import select, with_class, within
from hq.platform.core import seeds
from hq.platform.core.audit import ANONYMOUS
from hq.platform.core.example_estate.agents import AGENT, NOT_CARRIED
from hq.platform.core.example_estate.testing import sign_in, throwaway_media
from hq.platform.core.models import AgentIdentity, AuditLog, OwnDevice
from hq.platform.core.tests.browser.estate import ask_as_production, estate_pages, owner_client


class ExampleEstateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("standard") as owner:
            cls.user = owner

    def test_every_published_item_has_visits_and_the_site_a_full_quarter(self):
        self.assertTrue(analytics.coverage_summary(days=28)["complete"])
        published = ContentItem.objects.filter(content_type__in=WRITEUP_TYPES | PAGE_TYPES)
        self.assertTrue(published.filter(content_type__in=PAGE_TYPES).exists())
        for item in published:
            with self.subTest(item=item.slug):
                self.assertGreater(analytics.item_traffic(item)["pageviews"], 0)

    def test_each_breakdown_of_a_day_s_views_adds_up_to_them(self):
        # A visit always has a country, a device, a browser and a system; a
        # referrer it may lack.
        totals: dict[tuple, int] = {}
        for row in RumDaily.objects.exclude(dimension="referrer"):
            key = (row.date, row.dimension)
            totals[key] = totals.get(key, 0) + row.pageviews
        days = {day for day, _dimension in totals}
        self.assertTrue(days)
        for day in days:
            by = {dimension: views for (when, dimension), views in totals.items() if when == day}
            with self.subTest(day=day):
                self.assertEqual(set(by.values()), {by["path"]}, by)

    def test_the_contact_inbox_is_read_and_holds_mail(self):
        current = inbox.current()

        self.assertEqual(current.state, inbox.OK)
        self.assertTrue(current.rows)

    def test_the_sign_in_names_a_github_account_whose_stars_were_read(self):
        login = linked_login(self.user, GITHUB)

        self.assertTrue(login)
        self.assertTrue(github_profile.profile(login)["watched"])

    def test_the_audit_log_spans_months_by_the_operator_an_agent_and_the_scheduler(self):
        events = AuditLog.objects.all()
        now = timezone.now()

        self.assertLess(min(event.created_at for event in events), now - timedelta(days=180))
        self.assertLessEqual(max(event.created_at for event in events), now)
        self.assertLessEqual(
            {self.user.get_username(), AGENT, runner.SCHEDULER}, {event.actor_label for event in events}
        )

    def test_a_scheduled_run_that_changed_records_holds_what_it_changed(self):
        changed = [
            job
            for job in Job.objects.filter(actor=runner.SCHEDULER)
            if AuditLog.objects.filter(operation_id=str(job.pk)).exclude(object_id=str(job.pk)).count() > 1
        ]

        self.assertTrue(changed, "an event in the log opens to the records it changed")

    def test_the_owner_signs_in_to_a_gates_client_through_the_web(self):
        sign_in(Client(), self.user)

        login = AuditLog.objects.filter(action=AuditLog.Action.LOGIN).latest("pk")
        self.assertEqual((login.user, login.interface), (self.user, WEB_INTERFACE))

    def test_every_event_names_who_acted(self):
        nameless = [str(event) for event in AuditLog.objects.all() if event.actor_label == ANONYMOUS]

        self.assertEqual(nameless, [])

    def test_each_scheduled_run_is_one_row_dated_to_when_it_ended(self):
        for job in Job.objects.filter(actor=runner.SCHEDULER):
            with self.subTest(kind=job.kind):
                (ending,) = AuditLog.objects.filter(object_type=runner.AUDIT_TYPE, object_id=str(job.pk))
                self.assertEqual(ending.created_at, job.finished_at)

    def test_projects_were_not_all_edited_at_once(self):
        edited = {project.updated_at.date() for project in Project.objects.all()}

        self.assertGreater(len(edited), 1)
        self.assertLess(min(edited), timezone.localdate() - timedelta(days=1))

    def test_an_assets_expense_is_dated_and_filed_as_its_purchase(self):
        # An asset with no cost recorded was not bought, so no expense files it.
        for asset in Asset.objects.exclude(total_cost=0):
            with self.subTest(asset=asset.slug):
                (expense,) = Expense.objects.filter(related_asset=asset)
                self.assertEqual(expense.date, asset.purchase_date)
                self.assertNotEqual(asset.category, "other")

    def test_nothing_happened_before_the_server_was_bought(self):
        bought = min(asset.purchase_date for asset in Asset.objects.all())
        first = min(timezone.localdate(event.created_at) for event in AuditLog.objects.all())

        self.assertGreaterEqual(first, bought)

    def test_each_agent_and_device_event_is_dated_as_its_record_says(self):
        # The Agents page reads the record and the audit log reads the event;
        # both say when it happened.
        said = {}
        for identity in AgentIdentity.objects.all():
            said[(agent_registry.AUDIT_LABEL, identity.pk, AuditLog.Action.CREATED)] = identity.first_seen
            if identity.blocked_at:
                said[(agent_registry.AUDIT_LABEL, identity.pk, AuditLog.Action.UPDATED)] = identity.blocked_at
        for device in OwnDevice.objects.all():
            said[(own_devices.AUDIT_LABEL, device.pk, AuditLog.Action.CREATED)] = device.registered_at
        self.assertTrue(any(key[0] == own_devices.AUDIT_LABEL for key in said), "the estate registers a device")
        for (label, pk, action), moment in said.items():
            with self.subTest(label=label, pk=pk, action=action):
                events = AuditLog.objects.filter(object_type=label, object_id=str(pk), action=action)
                self.assertTrue(events.exists())
                for event in events:
                    self.assertEqual(timezone.localdate(event.created_at), timezone.localdate(moment))

    def test_some_spending_is_not_all_business(self):
        partial = Expense.objects.exclude(business_use_percentage=100)

        self.assertTrue(partial.exists())

    def test_the_agents_page_draws_every_state_a_rule_and_an_agent_can_be_in(self):
        client = Client()
        sign_in(client, self.user)
        page = client.get(reverse("agent_policy"), {"all": 1})

        # A rule is each state RULE_LABELS names, or the default.
        drawn = {
            name for each in with_class(page, "policy-select") for name in each.classes if name.startswith("rule-")
        }
        self.assertEqual(drawn, {f"rule-{state}" for state in ("default", *RULE_LABELS)})
        self.assertTrue(with_class(page, "policy-unavailable"), "a cell past an agent's grant")
        # An agent a viewer signed in: what of its grant is in force, and what is beyond their role.
        row = " ".join(element.words for element in within(page, "data-client", "example-reader"))
        self.assertTrue(within(page, "data-role", "viewer"))
        self.assertIn("Permissions: read", row)
        self.assertIn("beyond riley's role: write_calendar", row)
        # An agent whose last sign-in did not carry a permission HQ offers, named with its fix.
        missing = within(page, "data-missing")
        self.assertTrue(missing, "an agent missing a permission")
        self.assertIn(str(NOT_CARRIED), {element.words for element in missing if element.tag == "code"})

    def test_the_agents_page_draws_every_standing_a_device_can_be_in(self):
        from hq.platform.application.agent_devices import DeviceStanding
        from hq.platform.application.tests.markup import select

        client = Client()
        sign_in(client, self.user)
        page = client.get(reverse("agent_policy"))

        drawn = {row.attrs["data-device-standing"] for row in select(page, "data-device-standing")}
        # Unplaced is a call on no device, which has no row.
        self.assertEqual(drawn, {standing.value for standing in DeviceStanding} - {DeviceStanding.UNPLACED.value})
        self.assertTrue(select(page, "data-prompt"), "a device waits for the owner")

    def test_the_agents_held_request_reads_as_a_before_and_after(self):
        (held,) = approvals.pending(principal=cli_principal())
        preview = approvals.preview(held)

        self.assertEqual(held.requested_actor, AGENT)
        self.assertTrue(preview.compares, "a person reads what is there now beside what would be")
        self.assertTrue(preview.rows)
        self.assertFalse(preview.lines)

    def test_one_container_runs_by_tag_alone_and_is_asked_to_be_pinned_without_being_behind(self):
        def containers(rule):
            found = findings(principal=web_principal(self.user), rule=rule)["findings"]
            return [
                fact["value"]
                for finding in found
                if finding["rule"] == rule
                for fact in finding["evidence"]
                if fact["label"] == "Container"
            ]

        (container,) = containers("container-image-unpinned")

        self.assertNotIn(container, containers("container-image-behind"))


class WalkedStatesTests(TestCase):
    """Each state the estate keeps for the browser gate to read shows on a page its walk visits."""

    @classmethod
    def setUpTestData(cls):
        # The receipts' files stay where the seed wrote them, so a page finds them.
        cls.enterClassContext(throwaway_media())
        with seeds.built("showcase") as owner:
            cls.user = owner

    def setUp(self):
        sign_in(self.client, self.user)

    def walked(self, address):
        self.assertIn(address, estate_pages())
        page = self.client.get(address)
        self.assertEqual(page.status_code, 200)
        return page

    def test_a_name_no_declaration_claims_is_listed_with_its_adoption(self):
        from hq.platform.core.example_estate.network import HAND_MADE

        hostname = HAND_MADE["domain"]
        page = self.walked(reverse("control_plane:services"))

        self.assertIn(hostname, [service.hostname for service, _actions in page.context["unmanaged_rows"]])
        self.assertTrue(select(page, "formaction", reverse("control_plane:adopt", args=[hostname])))

    def test_the_unfiled_receipt_s_match_page_offers_its_expense_as_a_row(self):
        from hq.platform.core.example_estate.records import UNFILED, UNFILED_RECEIPT

        vendor, item = UNFILED[1:3]
        expense = Expense.objects.get(vendor=vendor, item=item)
        page = self.walked(reverse("receipts:match", args=[UNFILED_RECEIPT]))

        self.assertEqual([match for match, _actions in page.context["matches"]], [expense])
        self.assertIn(str(expense.pk), [field.attrs.get("value") for field in select(page, "name", "expense_id")])

    def test_the_photographed_receipt_s_page_draws_its_picture(self):
        from hq.platform.core.example_estate.records import UNFILED_RECEIPT

        page = self.walked(reverse("receipts:detail", args=[UNFILED_RECEIPT]))

        (part,) = select(page, "data-receipt-file")
        self.assertEqual(part.attrs["data-receipt-file"], "picture")
        picture = self.client.get(reverse("receipts:picture", args=[UNFILED_RECEIPT]))
        self.assertEqual(picture["Content-Type"], "image/png")
        self.assertTrue(b"".join(picture.streaming_content).startswith(b"\x89PNG\r\n\x1a\n"))

    def test_an_asset_with_nothing_beside_it_is_its_facts_alone(self):
        from hq.platform.core.example_estate.records import SWITCH_ASSET

        page = self.walked(reverse("assets:detail", kwargs={"slug": SWITCH_ASSET}))

        self.assertEqual(page.context["related"], ())
        self.assertFalse(page.context["asset"].notes)

    def test_the_reports_exports_follow_three_readings(self):
        # Three readings fill a wide row, so the exports wrap onto one of their own.
        page = self.walked(reverse("reports:dashboard"))

        for reading in ("status_groups", "assets_by_category", "docs_needing_review"):
            with self.subTest(reading=reading):
                self.assertTrue(page.context[reading])

    def test_two_lapsed_certificates_under_one_name_read_apart(self):
        from hq.platform.core.example_estate.infra import LEFT_OVER, LEFT_OVER_LAPSED

        self.walked(reverse("control_plane:findings"))
        found = findings(principal=web_principal(self.user), rule="certificate-expiring")["findings"]
        titles = [finding["title"] for finding in found if LEFT_OVER in finding["title"]]

        self.assertEqual(len(titles), 2, titles)
        self.assertEqual(len(set(titles)), 2, titles)
        for title in titles:
            self.assertIn(f"expired {LEFT_OVER_LAPSED.days} days ago", title)


class PinnedWordsTests(TestCase):
    def test_a_pinned_image_is_said_in_the_words_of_the_standard_s_check(self):
        from hq.platform.application.supply_chain import STANDARD

        (pinned,) = (check.label for check in STANDARD if check.id == "pinned")
        with owner_client() as client:
            container = ask_as_production(client, reverse("control_plane:detail", args=["home-automation-container"]))
            hq = ask_as_production(client, reverse("hq_self"))

        self.assertContains(container, pinned)
        self.assertContains(hq, pinned.lower())
