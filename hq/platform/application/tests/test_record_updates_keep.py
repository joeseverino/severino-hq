"""A write to a record that exists sets the fields its caller sent, and every
field left out keeps what the record holds; a new record takes its defaults."""

import json
from datetime import date
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from hq.domains.assets.models import Asset
from hq.domains.calendars.models import Entry
from hq.domains.expenses.models import Expense
from hq.domains.projects.models import Project

from ..action_links import command_url
from ..assets import serialize_asset
from ..capabilities import capability_registry, execute_capability
from ..integration_specs import capability_schema
from ..plugin_testing import form_keyed
from ..security import OPERATOR_CAPABILITIES, Principal

OPERATOR = Principal("operator", "cli", OPERATOR_CAPABILITIES)
# What an update's summary says of a field left out.
KEPT = "A field left out keeps its value."


def _run(name, payload, target=None):
    result = execute_capability(name, payload, principal=OPERATOR, target=target)
    assert result["ok"], result
    return result


class AnUpdateKeepsWhatItWasNotSentTests(TestCase):
    def setUp(self):
        Project.objects.create(name="Example", slug="example")

    def test_an_asset_update_keeps_a_number_and_its_links(self):
        _run(
            "asset.create",
            {
                "item_name": "Example box",
                "slug": "example-box",
                "business_use_percentage": 40,
                "related_projects": ["example"],
            },
        )

        _run("asset.update", {"item_name": "Example rack box"}, target="example-box")

        asset = Asset.objects.get(slug="example-box")
        self.assertEqual(asset.item_name, "Example rack box")
        self.assertEqual(asset.business_use_percentage, 40)
        self.assertEqual(list(asset.related_projects.values_list("slug", flat=True)), ["example"])

    def test_a_field_sent_is_set_even_to_its_default(self):
        _run("asset.create", {"item_name": "Example box", "slug": "example-box", "business_use_percentage": 40})

        _run("asset.update", {"business_use_percentage": 100}, target="example-box")

        self.assertEqual(Asset.objects.get(slug="example-box").business_use_percentage, 100)

    def test_an_asset_update_sent_only_its_cost_changes_only_its_cost(self):
        _run(
            "asset.create",
            {
                "item_name": "Example box",
                "slug": "example-box",
                "vendor": "Example Supply",
                "purchase_date": "2026-07-01",
                "business_use_percentage": 40,
                "notes": "Kept.",
                "related_projects": ["example"],
            },
        )
        before = serialize_asset(Asset.objects.get(slug="example-box"), relationships=True)

        after = _run("asset.update", {"total_cost": "12.00"}, target="example-box")["asset"]

        self.assertEqual(after["total_cost"], "12.00")
        derived = {"total_cost", "estimated_deductible_amount", "updated_at"}
        self.assertEqual(
            {name: value for name, value in after.items() if name not in derived},
            {name: value for name, value in before.items() if name not in derived},
        )

    def test_an_expense_update_needs_neither_its_date_nor_its_item_and_keeps_its_one_link(self):
        created = _run(
            "expense.create",
            {
                "date": "2026-07-25",
                "vendor": "Example",
                "item": "Thing",
                "business_use_percentage": 50,
                "related_project": "example",
            },
        )

        _run("expense.update", {"vendor": "Example Two"}, target=created["expense"]["id"])

        expense = Expense.objects.get()
        self.assertEqual(
            (expense.date, expense.item, expense.vendor, expense.business_use_percentage),
            (date(2026, 7, 25), "Thing", "Example Two", 50),
        )
        self.assertEqual(expense.related_project.slug, "example")

    def test_an_update_of_a_record_that_is_not_there_is_refused_as_not_found(self):
        result = execute_capability("asset.update", {"total_cost": "12.00"}, principal=OPERATOR, target="example-gone")

        self.assertEqual(result["error"]["code"], "not_found")

    def test_a_project_upsert_of_one_that_exists_keeps_what_it_was_not_sent(self):
        Project.objects.filter(slug="example").update(status=Project.Status.ACTIVE, notes="Kept.")

        result = _run("project.upsert", {"name": "Renamed", "slug": "example"})

        self.assertFalse(result["created"])
        project = Project.objects.get(slug="example")
        self.assertEqual((project.name, project.status, project.notes), ("Renamed", Project.Status.ACTIVE, "Kept."))

    def test_a_calendar_entry_update_keeps_how_it_repeats(self):
        created = _run(
            "calendar.entry.create",
            {
                "title": "Example review",
                "starts_on": "2026-07-01",
                "repeat": "weekly",
                "interval": 2,
                "weekdays": [0, 3],
            },
        )

        _run("calendar.entry.update", {"title": "Example retro"}, target=created["entry"]["uid"])

        entry = Entry.objects.get()
        self.assertEqual(
            (entry.title, entry.starts_on, entry.repeat, entry.interval, entry.weekdays),
            ("Example retro", date(2026, 7, 1), "weekly", 2, "0,3"),
        )

    def test_a_rule_across_two_fields_reads_the_entry_as_the_update_leaves_it(self):
        weekly = _run(
            "calendar.entry.create",
            {"title": "Example review", "starts_on": "2026-07-01", "repeat": "weekly", "weekdays": [0, 3]},
        )["entry"]["uid"]
        once = _run("calendar.entry.create", {"title": "Example launch", "starts_on": "2026-07-01"})["entry"]["uid"]

        for payload, target in (({"repeat": "daily"}, weekly), ({"repeat_until": "2026-08-01"}, once)):
            with self.subTest(payload):
                result = execute_capability("calendar.entry.update", payload, principal=OPERATOR, target=target)

                self.assertFalse(result["ok"], result)
        self.assertEqual(
            sorted(Entry.objects.values_list("repeat", "weekdays", "repeat_until")),
            [("", "", None), ("weekly", "0,3", None)],
        )
        _run("calendar.entry.update", {"repeat": "daily", "weekdays": []}, target=weekly)
        self.assertEqual(Entry.objects.get(uid=weekly).repeat, "daily")

    def test_a_new_record_takes_its_defaults(self):
        _run("asset.create", {"item_name": "Example box", "slug": "example-box"})
        _run("project.upsert", {"name": "Example new", "slug": "example-new"})

        self.assertEqual(Asset.objects.get(slug="example-box").business_use_percentage, 100)
        self.assertEqual(Project.objects.get(slug="example-new").status, Project.Status.IDEA)


class TheWebFormsWriteEveryFieldTests(TestCase):
    """A record's web form posts every field it shows, so a field left blank there is cleared."""

    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("operator", password="unused-test-pass"))

    def test_an_asset_form_clears_a_blank_field_and_sets_the_rest(self):
        Asset.objects.create(item_name="Example box", slug="example-box", notes="Old.", business_use_percentage=40)

        response = self.client.post(
            reverse("assets:edit", args=["example-box"]),
            form_keyed(
                {
                    "item_name": "Example box",
                    "slug": "example-box",
                    "category": "other",
                    "total_cost": "0.00",
                    "business_use_percentage": "100",
                    "status": "active",
                    "notes": "",
                }
            ),
        )

        self.assertEqual(
            response.status_code, 302, getattr(response, "context", None) and response.context["form"].errors
        )
        asset = Asset.objects.get(slug="example-box")
        self.assertEqual((asset.notes, asset.business_use_percentage), ("", 100))

    def test_a_calendar_form_clears_how_an_entry_repeats(self):
        entry = Entry.objects.create(title="Example review", starts_on=date(2026, 7, 1), repeat="daily", interval=3)

        response = self.client.post(
            reverse("calendar:entry_edit", args=[entry.uid]),
            form_keyed({"title": "Example review", "starts_on": "2026-07-01", "repeat": "", "interval": "1"}),
        )

        self.assertEqual(
            response.status_code, 302, getattr(response, "context", None) and response.context["form"].errors
        )
        entry.refresh_from_db()
        self.assertEqual((entry.repeat, entry.interval), ("", 1))


class TheCommandFormKeepsWhatIsLeftBlankTests(TestCase):
    """The generic command form posts every field it shows; a blank one an
    update may be sent without is left out, so the record keeps it."""

    def test_an_asset_update_form_filled_with_only_its_cost_changes_only_its_cost(self):
        self.client.force_login(get_user_model().objects.create_superuser("operator", password="unused-test-pass"))
        Asset.objects.create(item_name="Example box", slug="example-box", notes="Kept.", vendor="Example Supply")

        response = self.client.post(
            command_url("asset.update"),
            form_keyed({"__target": "example-box", "total_cost": "12.00", "item_name": "", "notes": "", "vendor": ""}),
        )

        self.assertEqual(response.status_code, 302, response.context and response.context["form"].errors)
        asset = Asset.objects.get(slug="example-box")
        self.assertEqual(
            (asset.total_cost, asset.item_name, asset.notes, asset.vendor),
            (Decimal("12.00"), "Example box", "Kept.", "Example Supply"),
        )


class TheOperatorsShellKeepsTests(TestCase):
    def test_an_asset_update_sent_only_its_cost_through_hq_call_changes_only_its_cost(self):
        Asset.objects.create(item_name="Example box", slug="example-box", notes="Kept.", business_use_percentage=40)
        request = {
            "tool": "execute_capability",
            "arguments": {"name": "asset.update", "payload": {"total_cost": "12.00"}, "target": "example-box"},
        }
        output = StringIO()

        with mock.patch("sys.stdin", StringIO(json.dumps(request))):
            call_command("hq_call", stdout=output)

        self.assertTrue(json.loads(output.getvalue())["ok"], output.getvalue())
        asset = Asset.objects.get(slug="example-box")
        self.assertEqual(
            (asset.total_cost, asset.item_name, asset.notes, asset.business_use_percentage),
            (Decimal("12.00"), "Example box", "Kept.", 40),
        )


class TheSchemaSaysAnUpdateKeepsTests(SimpleTestCase):
    def test_an_asset_update_says_a_field_left_out_keeps_its_value_and_shows_no_default(self):
        spec = capability_registry()["asset.update"]

        self.assertIn(KEPT, spec.summary)
        properties = capability_schema(spec)["properties"]
        self.assertEqual([name for name, field in properties.items() if "default" in field], [])

    def test_an_update_requires_nothing_it_is_sent_and_a_create_still_requires_its_name(self):
        registry = capability_registry()

        self.assertNotIn("required", capability_schema(registry["asset.update"]))
        self.assertEqual(capability_schema(registry["asset.create"])["required"], ["item_name"])
        self.assertEqual(capability_schema(registry["receipt.replace"])["required"], ["file"])

    def test_an_upsert_says_what_it_keeps_and_a_create_shows_its_defaults(self):
        registry = capability_registry()
        self.assertIn("a field left out keeps its value", registry["asset.upsert"].summary)
        self.assertNotIn(KEPT, registry["asset.create"].summary)
        properties = capability_schema(registry["asset.create"])["properties"]
        self.assertEqual(properties["business_use_percentage"]["default"], 100)
