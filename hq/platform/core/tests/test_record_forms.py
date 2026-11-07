"""A record's form reads as its command's page does: the create command's
words on its button, Save changes on one that exists, Cancel back, and each
field's help the command's meaning."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.domains.projects.models import Project
from hq.platform.application.capabilities import capability_registry
from hq.platform.application.describe import meanings
from hq.platform.application.tests.markup import select, within

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
