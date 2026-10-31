"""A command field that names records is a choice of those records."""

from django import forms
from django.contrib.auth import get_user_model
from django.test import TestCase

from hq.domains.assets.models import Asset
from hq.domains.content.models import ContentItem
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.docs_index.models import DocumentationRecord
from hq.domains.expenses.models import Expense
from hq.domains.projects.models import Project
from hq.platform.application.capabilities import capability_registry
from hq.platform.application.command_fields import named_records
from hq.platform.application.command_forms import command_form_class
from hq.platform.application.command_targets import CommandTargetOption, record_options
from hq.platform.application.integration_specs import capability_schema
from hq.platform.application.integrations import integration_graph
from hq.platform.application.references import NO_LONGER_OFFERED
from hq.platform.application.security import Capability, Principal
from hq.platform.application.tests.markup import select, within
from hq.platform.core.tests.estate_sizes import assert_cost_does_not_grow

User = get_user_model()


def option_values(page, name: str) -> list[str]:
    return [option.attrs["value"] for option in within(page, "name", name) if option.tag == "option"]


class RecordFieldTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser(
            username="record-fields",
            email="operator@example.test",
            password="strongtestpass-1234",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_every_record_field_names_a_listable_resource(self):
        resources = integration_graph().resources
        for name, spec in capability_registry().items():
            for field, schema in capability_schema(spec).get("properties", {}).items():
                declared = named_records(schema)
                if declared is None:
                    continue
                with self.subTest(command=name, field=field):
                    resource = resources.get(declared["resource"])
                    self.assertIsNotNone(resource)
                    self.assertIsNotNone(resource.list_handler)
                    self.assertTrue(declared.get("key") or resource.identifier)

    def test_refresh_offers_connections_and_reading_types_to_choose(self):
        page = self.client.get("/commands/infrastructure.controller.refresh/")

        for name in ("connection_ref", "kind"):
            with self.subTest(field=name):
                (control,) = select(page, "name", name)
                self.assertEqual(control.tag, "select")
        self.assertLessEqual(set(OBSERVATIONS), set(option_values(page, "kind")))

    def test_a_relation_offers_the_records_and_refuses_any_other(self):
        Project.objects.create(name="Example project", slug="example-project")
        page = self.client.get("/commands/asset.create/")
        self.assertIn("example-project", option_values(page, "related_projects"))
        key = page.context["form"].initial["__execution_key"]

        refused = self.client.post(
            "/commands/asset.create/",
            {"item_name": "Example", "related_projects": "missing-project", "__execution_key": key},
        )
        accepted = self.client.post(
            "/commands/asset.create/",
            {"item_name": "Example", "related_projects": "example-project", "__execution_key": key},
        )

        self.assertEqual(refused.status_code, 400)
        self.assertIn("related_projects", refused.context["form"].errors)
        self.assertEqual(accepted.status_code, 302)
        asset = Asset.objects.get(item_name="Example")
        self.assertEqual(list(asset.related_projects.values_list("slug", flat=True)), ["example-project"])

    def test_a_value_its_source_no_longer_lists_is_kept_under_its_own_heading(self):
        form_class = command_form_class(
            capability_registry()["expense.create"],
            record_choices={"related_project": (CommandTargetOption("example-live", "Live"),)},
        )

        form = form_class(initial={"related_project": "example-gone"})

        choices = dict(form.fields["related_project"].choices)
        self.assertEqual(dict(choices[NO_LONGER_OFFERED]), {"example-gone": "example-gone"})
        self.assertIn("example-live", choices)

    def test_a_field_whose_source_cannot_be_listed_is_typed(self):
        form_class = command_form_class(capability_registry()["expense.create"], record_choices={})

        self.assertNotIn("related_project", form_class.record_names)
        self.assertNotIsInstance(form_class.base_fields["related_project"], forms.ChoiceField)

    def test_the_choices_cost_the_same_however_many_records(self):
        principal = Principal("operator", "web", frozenset({Capability.READ}))
        schema = capability_schema(capability_registry()["content.create"])

        def arrange(count):
            for index in range(Project.objects.count(), count):
                slug = f"example-{index}"
                Project.objects.create(name=slug, slug=slug)
                Asset.objects.create(item_name=slug, slug=slug)
                ContentItem.objects.create(title=slug, slug=slug)
                DocumentationRecord.objects.create(doc_id=slug, title=slug)
                Expense.objects.create(date="2026-01-01", vendor="Example", item=slug)

        assert_cost_does_not_grow(
            self,
            sizes=(1, 5),
            arrange=arrange,
            draw=lambda _count: record_options(schema, principal=principal),
            rows=lambda offered: min(len(choices or ()) for choices in offered.values()),
        )
