"""A record's form reads as its command's page does: the create command's
words on its button, Save changes on one that exists, Cancel back, and each
field's help the command's meaning."""

from datetime import date

from django.core.exceptions import ImproperlyConfigured
from django.forms import ModelForm
from django.test import TestCase
from django.urls import reverse
from django.views.generic import CreateView

from hq.domains.assets.models import Asset
from hq.domains.projects.models import Project
from hq.platform.application.capabilities import capability_registry
from hq.platform.application.describe import meanings
from hq.platform.application.markup_testing import select, within
from hq.platform.application.plugin_testing import form_keyed
from hq.platform.application.routes import routed
from hq.platform.application.writes import CommandFormPage, RecordFormMixin
from hq.platform.core.testing import SignedInTestCase, operator


def creates() -> dict[str, str]:
    """Each page that makes a record from its form, by route, with the command it makes it by."""

    return {
        route.name: route.view().creates
        for route in routed()
        if isinstance(route.view, type)
        and issubclass(route.view, CreateView)
        and issubclass(route.view, (RecordFormMixin, CommandFormPage))
    }


def submits(page) -> list[str]:
    return [element.words for element in within(page, "data-submit-busy") if element.attrs.get("type") == "submit"]


def cancels(page) -> list[str]:
    return [element.attrs.get("href", "") for element in within(page, "data-submit-busy") if element.words == "Cancel"]


def help_of(page, name: str) -> str:
    shown = select(page, "id", f"id_{name}_helptext")
    return shown[0].words if shown else ""


class RecordFormTests(SignedInTestCase):
    def test_a_new_record_is_made_by_its_create_command_words(self):
        pages = creates()
        self.assertTrue(pages, "no page makes a record from its form")
        for route, capability in pages.items():
            with self.subTest(route=route):
                page = self.client.get(reverse(route))
                spec = capability_registry()[capability]

                # Its words, or a mode of them its declaration names.
                (said,) = submits(page)
                self.assertIn(said, {spec.words(), *dict(spec.variants).values()})
                self.assertEqual(len(cancels(page)), 1)
                self.assertTrue(cancels(page)[0])

    def test_a_field_says_what_its_command_says_of_it(self):
        for route, capability in creates().items():
            with self.subTest(route=route):
                page = self.client.get(reverse(route))
                form = page.context["form"]

                said = meanings(capability_registry()[capability].command_type)
                shown = {name: help_of(page, name) for name in said if name in form.fields}
                self.assertTrue(shown)
                # A choice with nothing to choose says so after it.
                self.assertEqual(
                    {name: words[: len(said[name])] for name, words in shown.items()},
                    {name: said[name] for name in shown},
                )

    def test_a_record_that_exists_is_saved_as_changes_and_cancelled_back_to_itself(self):
        project = Project.objects.create(name="Example project", slug="example-project")

        page = self.client.get(reverse("projects:edit", args=[project.slug]))

        self.assertEqual(submits(page), ["Save changes"])
        self.assertEqual(cancels(page), [project.get_absolute_url()])


class DerivedRecordFormTests(TestCase):
    """A record's form is its command's fields, and what it posts is saved."""

    def test_a_field_posted_on_the_form_is_saved(self):
        operator(self.client, "example-saver")

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

    def test_a_record_view_that_names_a_form_of_its_own_is_refused_as_it_is_declared(self):
        from hq.platform.application.writes import RecordFormMixin

        with self.assertRaises(ImproperlyConfigured):
            type("NamesAForm", (RecordFormMixin, CreateView), {"model": Asset, "form_class": ModelForm})

    def test_a_slug_sent_with_spaces_around_it_is_saved_without_them(self):
        operator(self.client, "example-slugger")

        self.client.post(
            reverse("projects:create"),
            form_keyed({"name": "Example padded", "slug": "  example-padded  ", "category": "other", "status": "idea"}),
        )

        self.assertTrue(Project.objects.filter(slug="example-padded").exists())
