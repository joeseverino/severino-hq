"""The example estate reads like an HQ that has run for months: every page the
navigation offers has something on it, and its history is spread over that
time by the people and machines that made it."""

from datetime import timedelta
from urllib.parse import urljoin, urlsplit

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.analytics.models import RumDaily
from hq.domains.assets.models import Asset
from hq.domains.contacts import inbox
from hq.domains.content.models import PAGE_TYPES, WRITEUP_TYPES, ContentItem
from hq.domains.control_plane.models import OperationRequest, OperationState, SourceProposal
from hq.domains.expenses.models import Expense
from hq.domains.jobs import runner
from hq.domains.jobs.models import Job
from hq.domains.projects.models import Project
from hq.platform.application import agent_registry, analytics, approval_review, approvals, github_profile, own_devices
from hq.platform.application.automation_adoption import ADOPT
from hq.platform.application.capability_policy import RULE_LABELS
from hq.platform.application.findings import findings
from hq.platform.application.linked_accounts import GITHUB, linked_login
from hq.platform.application.security import WEB_INTERFACE, cli_principal, web_principal
from hq.platform.application.tests.markup import all_elements, elements, select, with_class, within
from hq.platform.application.topology import derive_topology
from hq.platform.core import seeds
from hq.platform.core.audit import ANONYMOUS
from hq.platform.core.example_estate import automation
from hq.platform.core.example_estate.agents import AGENT, NOT_CARRIED, REWRITE
from hq.platform.core.example_estate.testing import sign_in, throwaway_media
from hq.platform.core.models import AgentIdentity, AuditLog, OwnDevice
from hq.platform.core.tests.browser.estate import ask_as_production, estate_pages, landings, owner_client


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
        from hq.platform.application.capability_policy import RULE_TONES

        client = Client()
        sign_in(client, self.user)
        page = client.get(reverse("agent_policy"), {"all": 1})

        # A rule is each state RULE_LABELS names, or the default, each said in its tone.
        chosen = [
            element
            for element in within(page, "data-policy")
            if element.tag == "option" and "selected" in element.attrs
        ]
        self.assertEqual({option.attrs["value"] for option in chosen}, {"", *RULE_LABELS})
        for option in chosen:
            (setting,) = [element for element in option.ancestors if element.tag == "select"]
            self.assertIn(f"toned-{RULE_TONES.get(option.attrs['value'], 'neutral')}", setting.classes)
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
        (held,) = (held for held in approvals.pending(principal=cli_principal()) if held.target == REWRITE)
        preview = approval_review.preview(held)

        self.assertEqual(held.requested_actor, AGENT)
        self.assertTrue(preview.compares, "a person reads what is there now beside what would be")
        self.assertTrue(preview.rows)
        self.assertFalse(preview.lines)

    def test_the_agents_held_adoption_warns_of_what_it_widens_and_reads_each_procedure_apart(self):
        (held,) = (held for held in approvals.pending(principal=cli_principal()) if held.capability == ADOPT)
        shown = approval_review.review(held)
        groups = {group.label: group for group in shown["preview"].groups}

        self.assertTrue(shown["warnings"], "a widening is said before anything is approved")
        self.assertEqual(set(groups), {procedure["title"] for procedure in automation.PROCEDURES + automation.WIDENED})
        self.assertTrue(any(group.changed for group in groups.values()))
        self.assertTrue(
            any(not group.changed for group in groups.values()), "a procedure that stays is shown as it stands"
        )

    def test_each_held_request_s_card_links_the_record_it_acts_on_by_name(self):
        client = Client()
        sign_in(client, self.user)

        for held in approvals.pending(principal=cli_principal()):
            with self.subTest(capability=held.capability):
                page = client.get(reverse("core:approval_entry", args=[held.pk]), follow=True)

                record = approval_review.review(held)["resource_url"]
                self.assertEqual(page.status_code, 200)
                self.assertTrue(record, "every held request the estate seeds acts on a record")
                self.assertTrue(
                    [link for link in elements(page, "a") if link.attrs.get("href") == record and link.words],
                    "the aside's link says where it goes",
                )

    def test_the_held_adoption_s_card_links_the_procedures_page(self):
        (held,) = (held for held in approvals.pending(principal=cli_principal()) if held.capability == ADOPT)
        self.assertEqual(approval_review.review(held)["resource_url"], reverse("control_plane:procedures"))

    def test_the_repository_was_scanned_into_a_proposal_hq_cannot_adopt_and_once_failed(self):
        scans = OperationRequest.objects.filter(action=OperationRequest.Action.SCAN)

        self.assertTrue(
            any(procedure["problems"] for proposal in SourceProposal.objects.all() for procedure in proposal.procedures)
        )
        self.assertTrue(scans.filter(state=OperationState.FAILED).exists())

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

    def test_each_folded_part_a_walked_page_holds_is_walked_opened(self):
        # A fold's part is drawn only once opened, which the walk's load never
        # does, so the page drawn with it open (the address its placeholder falls
        # back to, the page's own with a query) is walked in its own right. A
        # fallback to another page is that page's to walk.
        walked = landings(self.client.get, estate_pages())
        opened = (
            urlsplit(urljoin(address, region.attrs["data-fragment-fallback"]))._replace(fragment="")
            for address in walked
            for region in select(self.client.get(address), "data-fragment-fallback")
        )
        folded = {page.geturl() for page in opened if page.query and page.path in {urlsplit(at).path for at in walked}}

        self.assertIn(f"{reverse('agent_policy')}?endpoint=1", folded)
        self.assertLessEqual(folded, set(walked))

    def test_what_the_controller_reads_of_its_host_is_on_the_machine_it_runs_on(self):
        # Production's controller reports as its machine's hostname, so what it
        # reads of that machine is said on the machine's page. The estate's
        # runs on its one machine, beside HQ.
        from hq.platform.core.example_estate.names import MACHINE as CONTROLLER

        principal = web_principal(self.user)
        nodes = derive_topology(principal=principal).nodes
        (machine,) = [node.id for node in nodes if node.kind == "machine" and node.label == CONTROLLER]
        host = {
            finding["rule"]: finding["subject"]
            for finding in findings(principal=principal)["findings"]
            if (finding["scope"] or "").startswith("host.")
        }
        page = self.walked(reverse("control_plane:machine", kwargs={"name": CONTROLLER}))

        self.assertEqual([node.id for node in nodes if node.kind == "controller"], [])
        self.assertLessEqual({"filesystem-nearly-full", "reboot-pending"}, set(host))
        for rule, subject in host.items():
            with self.subTest(rule=rule):
                self.assertEqual(subject, machine)
                self.assertContains(page, f"rule%3D{rule}")

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

    def test_a_queue_row_holds_buttons_past_the_ones_its_line_shows(self):
        # The queue's row shows its first two buttons; the rest wait in its
        # opened line, a part the survey reaches only through a row with more.
        page = self.walked(reverse("action_items"))

        waiting = [
            control
            for control in within(page, "class", "row-later")
            if control.tag in {"a", "button"} and any("data-attention-item" in row.attrs for row in control.ancestors)
        ]
        self.assertTrue(waiting)

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

    def test_a_name_one_reading_holds_twice_is_one_item_and_no_source_repeats_a_key(self):
        # The queue knows an item by its key; a source answering two under one
        # has given it one item it cannot tell apart, row or dismissal.
        from hq.domains.control_plane.models import ProviderInventory
        from hq.platform.application.action_items import item_key
        from hq.platform.application.domains import all_domains
        from hq.platform.application.inventory_testing import store
        from hq.platform.application.projection import projection_scope

        # An edge certificate whose renewal is failing, beside the one it
        # replaced, which has run out: one name held twice by one reading.
        kind = "cloudflare.edge_certificate"
        (edge, *_rest) = ProviderInventory.objects.get(kind=kind).records
        held_twice = f"legacy.{edge['zone']}"
        store(
            kind,
            edge,
            *(
                {**edge, "id": f"example-advanced-{days}", "hosts": [held_twice], "expires_on": when}
                for days, when in ((5, in_days(5)), (-3, in_days(-3)))
            ),
        )

        page = self.walked(reverse("action_items"))

        ids = [element.attrs["id"] for element in all_elements(page) if element.attrs.get("id")]
        self.assertEqual(sorted({name for name in ids if ids.count(name) > 1}), [])
        for fold in select(page, "data-fold-toggle"):
            self.assertTrue(fold.attrs.get("aria-label"), fold)
        # The name is judged by the certificate that serves it, the newer one.
        (item,) = [
            item
            for group in page.context["action_groups"]
            for item in group["items"]
            if item["source_id"] == "hq.estate" and held_twice in item["label"]
        ]
        self.assertIn("expires in 5 days", item["label"])
        with projection_scope():
            for domain in all_domains():
                if domain.integration.attention is None:
                    continue
                keys = [item_key(domain.id, item) for item in domain.integration.attention()]
                with self.subTest(source=domain.id):
                    self.assertEqual(sorted({key for key in keys if keys.count(key) > 1}), [])


def in_days(days):
    return (timezone.now() + timedelta(days=days, hours=1)).isoformat()


class PinnedWordsTests(TestCase):
    def test_a_pinned_image_is_said_in_the_words_of_the_standard_s_check(self):
        from hq.platform.application.supply_chain import STANDARD

        (pinned,) = (check.label for check in STANDARD if check.id == "pinned")
        with owner_client() as client:
            container = ask_as_production(client, reverse("control_plane:detail", args=["home-automation-container"]))
            hq = ask_as_production(client, reverse("hq_self"))

        self.assertContains(container, pinned)
        self.assertContains(hq, pinned.lower())
