"""The audit pages: the log, a change, one entry, and the registry command."""

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.bridge_actions import ACTIONS
from hq.domains.control_plane.models import ManagedResource
from hq.domains.expenses.models import Expense
from hq.domains.projects.models import Project
from hq.platform.application.markup_testing import hrefs, text, with_class, within
from hq.platform.core.audit import audited_labels
from hq.platform.core.bindings import operation_context
from hq.platform.core.models import AuditLog
from hq.platform.core.testing import SignedInTestCase, declare


class AuditLogTests(TestCase):
    def test_create_writes_audit_event(self):
        before = AuditLog.objects.count()
        Project.objects.create(name="audited", status=Project.Status.IDEA)
        # Audit signals attribute to no user when called outside a request,
        # but the event itself should be written.
        self.assertEqual(AuditLog.objects.count(), before + 1)
        event = AuditLog.objects.first()
        self.assertEqual(event.object_type, "Project")
        self.assertEqual(event.action, AuditLog.Action.CREATED)


class AuditChangeTests(TestCase):
    """An update has to say what changed, or it is only saying that it did."""

    def test_an_update_records_which_fields_moved_from_the_last_save_and_to_what(self):
        project = Project.objects.create(name="Before", status=Project.Status.IDEA)
        project.name = "After"
        project.status = Project.Status.ACTIVE
        project.save()
        project.name = "Again"
        project.save()

        first, second = AuditLog.objects.filter(action=AuditLog.Action.UPDATED).order_by("pk")
        self.assertEqual(first.metadata["changes"], {"name": ["Before", "After"], "status": ["idea", "active"]})
        self.assertEqual(second.metadata["changes"], {"name": ["After", "Again"]})

    def test_a_save_that_changed_nothing_is_not_an_event(self):
        # Django writes every column on every save; an unchanged re-submission
        # records no event.
        project = Project.objects.create(name="Steady", status=Project.Status.IDEA)
        before = AuditLog.objects.count()

        project.save()

        self.assertEqual(AuditLog.objects.count(), before)

    def test_values_that_are_not_json_survive_the_round_trip(self):
        # A Decimal, a date and a UUID are none of the things JSON has. Left
        # as they are the row fails to write, `record_event` swallows it, and
        # the change is lost with nothing saying so.
        expense = Expense.objects.create(date=timezone.localdate(), vendor="V", item="I", total_cost=Decimal("1.00"))
        expense.total_cost = Decimal("2.50")
        expense.save()

        event = AuditLog.objects.filter(action=AuditLog.Action.UPDATED, object_type="Expense").first()
        self.assertIsNotNone(event, "the row must be written, not swallowed")
        self.assertEqual(event.metadata["changes"]["total_cost"], ["1.00", "2.50"])

    def test_a_redacted_field_reports_the_change_without_the_value(self):
        from hq.platform.core.audit import REDACTED, _changes

        changes = _changes({"token": "old-secret"}, {"token": "new-secret"}, frozenset({"token"}))

        # That it rotated is worth recording. What it rotated to is not.
        self.assertEqual(changes["token"], [REDACTED, REDACTED])


class AuditDetailPageTests(SignedInTestCase):
    """Every row leads somewhere, and what it leads to answers the question."""

    def test_an_event_shows_the_fields_that_moved(self):
        project = Project.objects.create(name="Before", status=Project.Status.IDEA)
        project.name = "After"
        project.save()
        event = AuditLog.objects.filter(action=AuditLog.Action.UPDATED).first()

        page = self.client.get(reverse("core:audit_detail", args=[event.pk]))

        shown = text(page)
        self.assertIn("Before", shown)
        self.assertIn("After", shown)
        self.assertEqual(page.context["page"].trail, (("Audit log", reverse("core:audit_list")),))
        # What else happened to the same object: its creation.
        created = AuditLog.objects.get(object_id=str(project.pk), action=AuditLog.Action.CREATED)
        self.assertIn(reverse("core:audit_detail", args=[created.pk]), hrefs(page))

    def test_a_record_field_that_moved_is_said_in_words(self):
        from hq.domains.control_plane.models import ManagedResource

        resource = declare(
            "example-policy",
            "example.policy",
            {"grants": [{"src": ["group:example"], "dst": ["tag:example"]}], "hosts": {"one": "192.0.2.1"}},
        )
        resource = ManagedResource.objects.get(pk=resource.pk)
        resource.spec["grants"][0]["src"].append("group:elsewhere")
        resource.spec["hosts"]["one"] = "192.0.2.2"
        with operation_context(interface="cli", actor="op", operation="example.edit"):
            resource.save()
        event = AuditLog.objects.get(action=AuditLog.Action.UPDATED, object_id=str(resource.pk))

        page = self.client.get(reverse("core:audit_detail", args=[event.pk]))
        shown = text(page)

        self.assertIn("Spec › Grants › group:example › Src", shown)
        self.assertIn("group:example, group:elsewhere", shown)
        self.assertIn("Spec › Hosts › One", shown)
        self.assertIn("192.0.2.2", shown)
        self.assertNotIn("{'", shown)
        self.assertNotIn("['", shown)
        # Nothing else was recorded beside the change but the record it is
        # about: the Who and Operation rows say the rest.
        self.assertEqual(set(page.context["extra"]), set())

    def test_the_event_and_what_it_recorded_are_sections_of_one_flow(self):
        # Sibling sections share a row where they fit (Design, "Sibling
        # sections flow"), so a short list of facts never crosses the page.
        event = AuditLog.objects.create(
            action=AuditLog.Action.UPDATED,
            object_type="Project",
            object_repr="Example",
            metadata={"changes": {"name": ["Before", "After"]}, "example": "value"},
        )

        page = self.client.get(reverse("core:audit_detail", args=[event.pk]))

        sections = [element for element in with_class(page, "section") if element.tag == "section"]
        flows = [[id(a) for a in section.ancestors if "section-flow" in a.classes] for section in sections]
        self.assertEqual(len(sections), 3, [section.words[:40] for section in sections])
        self.assertTrue(all(flows), flows)
        self.assertEqual(len({flow[-1] for flow in flows}), 1)

    def test_an_event_links_to_the_rest_of_its_operation(self):
        # The point of operation_id: one action touching several rows is one
        # thing that happened, not four events that share a timestamp.
        with operation_context(interface="cli", actor="op", operation="test.bulk", operation_id="op-1"):
            Project.objects.create(name="One", status=Project.Status.IDEA)
            Project.objects.create(name="Two", status=Project.Status.IDEA)
        event, other = AuditLog.objects.filter(operation_id="op-1")

        page = self.client.get(reverse("core:audit_detail", args=[event.pk]))

        self.assertIn(reverse("core:audit_detail", args=[other.pk]), hrefs(page))

    def test_the_operation_and_the_objects_history_are_one_table_whose_history_folds(self):
        with operation_context(interface="cli", actor="op", operation="test.bulk", operation_id="op-1"):
            project = Project.objects.create(name="One", status=Project.Status.IDEA)
            Project.objects.create(name="Two", status=Project.Status.IDEA)
        project.name = "Renamed"
        project.save()
        created = AuditLog.objects.get(object_id=str(project.pk), action=AuditLog.Action.CREATED)
        updated = AuditLog.objects.get(object_id=str(project.pk), action=AuditLog.Action.UPDATED)

        page = self.client.get(reverse("core:audit_detail", args=[created.pk]))

        inside = within(page, "class", "table-scroll")
        self.assertEqual(len([each for each in inside if each.tag == "table"]), 1)
        self.assertEqual(len([each for each in inside if each.attrs.get("scope") == "rowgroup"]), 2)
        folds = {each.attrs["id"] for each in inside if "data-folds" in each.attrs}
        # The object's own history folds away under its heading, as each step of the chain folds what it changed.
        (link,) = [each for each in inside if each.attrs.get("href") == reverse("core:audit_detail", args=[updated.pk])]
        (row,) = [ancestor for ancestor in link.ancestors if "data-folded" in ancestor.attrs]
        self.assertIn(row.attrs["data-folded"], folds)

    def test_a_row_written_before_rows_named_their_record_still_names_it(self):
        resource = declare("example-older", "example.record")
        label = audited_labels()[ManagedResource]
        older = AuditLog.objects.create(
            action=AuditLog.Action.UPDATED, object_type=label, object_id=str(resource.pk), object_repr="example-older"
        )

        page = self.client.get(reverse("core:audit_detail", args=[older.pk]))

        self.assertIn(f"{reverse('core:audit_list')}?about=example-older", hrefs(page))

    def test_a_row_whose_record_is_gone_or_unreadable_names_none(self):
        label = audited_labels()[ManagedResource]
        for object_id in ("00000000-0000-7000-8000-000000000000", "not-a-key"):
            with self.subTest(object_id=object_id):
                row = AuditLog.objects.create(action=AuditLog.Action.DELETED, object_type=label, object_id=object_id)

                page = self.client.get(reverse("core:audit_detail", args=[row.pk]))

                self.assertEqual(page.status_code, 200)
                self.assertNotIn("about", page.context)

    def test_an_operation_is_titled_in_the_words_that_declared_it_and_never_by_its_name(self):
        claim = next(action for action in ACTIONS if action.name == "claim")
        for operation, interface, said, unsaid in (
            ("bridge.claim", "controller", claim.title, "Bridge Claim"),
            ("example.untitled", "cli", "", "Example Untitled"),
        ):
            with self.subTest(operation=operation):
                # Two rows, so the log's line is named by its operation rather than by one row's record.
                row, _other = (
                    AuditLog.objects.create(
                        action=AuditLog.Action.UPDATED,
                        object_type="example.record",
                        object_repr=f"example-{letter}",
                        operation_id=operation,
                        metadata={"operation": operation, "interface": interface, "actor": "example-actor"},
                    )
                    for letter in "ab"
                )

                shown = [text(self.client.get(reverse("core:audit_detail", args=[row.pk])))]
                shown.append(text(self.client.get(reverse("core:audit_list"))))

                for page in shown:
                    self.assertNotIn(unsaid, page)
                    if said:
                        self.assertIn(said, page)

    def test_a_redacted_field_shows_that_it_changed_and_never_its_value(self):
        row = AuditLog.objects.create(
            action=AuditLog.Action.UPDATED,
            object_type="example.record",
            metadata={"changes": {"token": ["«redacted»", "«redacted»"]}},
        )

        page = self.client.get(reverse("core:audit_detail", args=[row.pk]))

        self.assertEqual([change.label for change in page.context["changes"].rows], ["Token"])
        self.assertNotIn("«redacted»", text(page))
