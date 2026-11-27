"""A write to a record that exists sets the fields its caller sent, and every
field left out keeps what the record holds; a new record takes its defaults."""

from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from hq.domains.assets.models import Asset
from hq.domains.calendars.models import Entry
from hq.domains.projects.models import Project
from hq.platform.core.testing import SignedInTestCase, operator

from ..action_links import command_url
from ..capabilities import execute_capability
from ..plugin_testing import form_keyed
from ..security import OPERATOR_CAPABILITIES, Principal

OPERATOR = Principal("operator", "cli", OPERATOR_CAPABILITIES)


def _run(name, payload, target=None):
    result = execute_capability(name, payload, principal=OPERATOR, target=target)
    assert result["ok"], result
    return result


class AnUpdateKeepsWhatItWasNotSentTests(TestCase):
    def test_a_field_sent_is_set_even_to_its_default(self):
        _run("asset.create", {"item_name": "Example box", "slug": "example-box", "business_use_percentage": 40})

        _run("asset.update", {"business_use_percentage": 100}, target="example-box")

        self.assertEqual(Asset.objects.get(slug="example-box").business_use_percentage, 100)

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
        _run("project.create", {"name": "Example new", "slug": "example-new"})

        self.assertEqual(Asset.objects.get(slug="example-box").business_use_percentage, 100)
        self.assertEqual(Project.objects.get(slug="example-new").status, Project.Status.IDEA)


class TheWebFormsWriteEveryFieldTests(SignedInTestCase):
    """A record's web form posts every field it shows, so a field left blank there is cleared."""

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
        operator(self.client)
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
