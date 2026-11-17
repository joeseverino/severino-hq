"""A record's form reads as its command's page does: the create command's
words on its button, Save changes on one that exists, Cancel back, and each
field's help the command's meaning."""

from dataclasses import dataclass, replace
from datetime import date

from django.contrib.auth import get_user_model
from django.core.exceptions import FieldError, ImproperlyConfigured
from django.forms import ModelForm
from django.test import TestCase
from django.urls import reverse
from django.views.generic import CreateView

from hq.domains.assets.models import Asset
from hq.domains.projects.models import Project
from hq.platform.application.capabilities import capability_registry
from hq.platform.application.describe import meanings
from hq.platform.application.domains import records_of
from hq.platform.application.markup_testing import select, within
from hq.platform.application.plugin_testing import form_keyed

# Each page that makes a record from its form, and the command it makes it by.
CREATES = (
    ("assets:create", "asset.create"),
    ("expenses:create", "expense.create"),
    ("projects:create", "project.create"),
    ("docs_index:create", "documentation.create"),
    ("calendar:entry_new", "calendar.entry.create"),
    ("receipts:create", "receipt.upload"),
)


def submits(page) -> list[str]:
    return [element.words for element in within(page, "data-submit-busy") if element.attrs.get("type") == "submit"]


def cancels(page) -> list[str]:
    return [element.attrs.get("href", "") for element in within(page, "data-submit-busy") if element.words == "Cancel"]


def help_of(page, name: str) -> str:
    shown = select(page, "id", f"id_{name}_helptext")
    return shown[0].words if shown else ""


class RecordFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser("example-operator", "operator@example.test", "x" * 20)

    def setUp(self):
        self.client.force_login(self.user)

    def test_a_new_record_is_made_by_its_create_command_words(self):
        for route, capability in CREATES:
            with self.subTest(route=route):
                page = self.client.get(reverse(route))

                self.assertEqual(submits(page), [capability_registry()[capability].words()])
                self.assertEqual(len(cancels(page)), 1)
                self.assertTrue(cancels(page)[0])

    def test_a_field_says_what_its_command_says_of_it(self):
        for route, capability in CREATES:
            with self.subTest(route=route):
                page = self.client.get(reverse(route))
                form = page.context["form"]
                written = {*(form._meta.help_texts or {})} | {
                    name for name, field in form.declared_fields.items() if field.help_text
                }

                said = meanings(capability_registry()[capability].command_type)
                shown = {name: help_of(page, name) for name in said if name in form.fields and name not in written}
                self.assertTrue(shown)
                self.assertEqual(shown, {name: said[name] for name in shown})

    def test_a_record_that_exists_is_saved_as_changes_and_cancelled_back_to_itself(self):
        project = Project.objects.create(name="Example project", slug="example-project")

        page = self.client.get(reverse("projects:edit", args=[project.slug]))

        self.assertEqual(submits(page), ["Save changes"])
        self.assertEqual(cancels(page), [project.get_absolute_url()])


@dataclass(frozen=True, slots=True)
class TakesAWarranty:
    item_name: str
    warranty_date: date | None = None


@dataclass(frozen=True, slots=True)
class TakesWhatNoColumnHolds:
    item_name: str
    nowhere: str = ""


class ListsItsOwnFields(ModelForm):
    class Meta:
        fields = ("item_name",)


def declared(command: type):
    return replace(records_of("assets"), command=f"{__name__}:{command.__name__}")


class DerivedRecordFormTests(TestCase):
    """A record's form is its command's fields, each drawn from the model's column."""

    def test_a_field_the_command_takes_is_on_the_form_in_its_columns_control(self):
        from hq.platform.application.records import record_form

        form = record_form(declared(TakesAWarranty))()

        self.assertEqual(list(form.fields), ["item_name", "warranty_date"])
        self.assertEqual(form.fields["warranty_date"].widget.input_type, "date")

    def test_a_field_no_column_holds_is_refused_as_the_form_is_built(self):
        from hq.platform.application.records import record_form

        with self.assertRaises(FieldError):
            record_form(declared(TakesWhatNoColumnHolds))

    def test_a_field_posted_on_the_form_is_saved(self):
        user = get_user_model().objects.create_superuser("example-saver", "saver@example.test", "x" * 20)
        self.client.force_login(user)

        self.client.post(
            reverse("assets:create"),
            form_keyed(
                {
                    "item_name": "Example asset",
                    "category": "other",
                    "total_cost": "1.00",
                    "business_use_percentage": "100",
                    "status": "active",
                    "warranty_date": "2030-01-31",
                }
            ),
        )

        self.assertEqual(Asset.objects.get(item_name="Example asset").warranty_date, date(2030, 1, 31))

    def test_a_form_beneath_it_that_lists_fields_of_its_own_is_refused(self):
        from hq.platform.application.records import record_form

        with self.assertRaises(ImproperlyConfigured):
            record_form(replace(records_of("assets"), form=f"{__name__}:ListsItsOwnFields"))

    def test_a_record_view_that_names_a_form_of_its_own_is_refused_as_it_is_declared(self):
        from hq.platform.application.writes import RecordFormMixin

        with self.assertRaises(ImproperlyConfigured):
            type("NamesAForm", (RecordFormMixin, CreateView), {"model": Asset, "form_class": ModelForm})

    def test_a_slug_sent_with_spaces_around_it_is_saved_without_them(self):
        user = get_user_model().objects.create_superuser("example-slugger", "slugger@example.test", "x" * 20)
        self.client.force_login(user)

        self.client.post(
            reverse("projects:create"),
            form_keyed({"name": "Example padded", "slug": "  example-padded  ", "category": "other", "status": "idea"}),
        )

        self.assertTrue(Project.objects.filter(slug="example-padded").exists())
