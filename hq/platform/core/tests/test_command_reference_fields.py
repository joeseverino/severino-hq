"""A command field holding a reference offers what the record's own form offers."""

from unittest import mock

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from hq.domains.assets.models import Asset
from hq.domains.calendars.models import Entry
from hq.domains.control_plane.models import ProviderConnection
from hq.domains.control_plane.providers import PROVIDERS
from hq.domains.expenses.forms import ExpenseForm
from hq.domains.expenses.models import Expense
from hq.domains.projects.models import Project
from hq.platform.application import references
from hq.platform.application.capabilities import capability_registry
from hq.platform.application.command_forms import command_form_class
from hq.platform.application.command_targets import record_options
from hq.platform.application.integration_specs import capability_schema
from hq.platform.application.references import Referable, Target
from hq.platform.application.security import Principal, web_principal
from hq.platform.application.tests.markup import within

User = get_user_model()

# The host's commands whose field is a type:identity reference, and the column each fills.
HOST_REFERENCES = {
    ("asset.create", "infrastructure"): (Asset, "infrastructure"),
    ("expense.create", "paid_from"): (Expense, "paid_from"),
    ("expense.create", "about"): (Expense, "about"),
    ("calendar.entry.create", "about"): (Entry, "about"),
}


def offered(page, name: str) -> dict[str, str]:
    """Each option a field's select offers, by value, with the group it is under."""

    found = {}
    for option in within(page, "name", name):
        if option.tag == "option" and option.attrs.get("value"):
            group = next((outer.attrs.get("label", "") for outer in option.ancestors if outer.tag == "optgroup"), "")
            found[option.attrs["value"]] = group
    return found


def grouped(field, principal: Principal) -> dict[str, str]:
    """What the record's own form offers for one reference column, by value, with its heading."""

    return {
        value: heading
        for heading, group in references.choices(field, principal=principal)
        if isinstance(group, list)
        for value, _label in group
    }


def reference_fields() -> dict[tuple[str, str], tuple[type, str]]:
    """``(model, column)`` by ``(command, field)`` for every command field the
    machine contract says holds a reference (``x-hq-reference``)."""

    found = {}
    for name, spec in capability_registry().items():
        for field, schema in capability_schema(spec).get("properties", {}).items():
            if isinstance(reference := schema.get("x-hq-reference"), dict):
                found[name, field] = (apps.get_model(reference["model"]), reference["field"])
    return found


class ReferenceFieldTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("operator", "operator@example.com", "a-password")
        ProviderConnection.objects.create(
            connection_ref="a-portainer",
            controller_id="a-controller",
            provider="portainer",
            endpoint="https://portainer.example.com",
            reaches=["lab-1"],
            reachable=True,
            probed=True,
            observed_at=timezone.now(),
        )
        Project.objects.create(name="An Example Project", slug="an-example")
        Asset.objects.create(item_name="A router", slug="a-router")

    def setUp(self):
        self.client.force_login(self.user)

    def test_every_type_identity_field_says_which_column_it_fills(self):
        self.assertLessEqual(HOST_REFERENCES.items(), reference_fields().items())

    def test_each_command_form_offers_what_the_record_form_offers(self):
        principal = web_principal(self.user)
        for (name, field), (model, column) in {**HOST_REFERENCES, **reference_fields()}.items():
            with self.subTest(command=name, field=field):
                page = self.client.get(f"/commands/{name}/")
                expected = grouped(model._meta.get_field(column), principal)

                self.assertEqual(set(offered(page, field)), set(expected))

    def test_the_picker_offers_infrastructure_and_records_under_their_headings(self):
        asset = offered(self.client.get("/commands/asset.create/"), "infrastructure")
        event = offered(self.client.get("/commands/calendar.entry.create/"), "about")

        self.assertIn("machine:lab-1", asset)
        self.assertNotIn("project:an-example", asset)
        self.assertEqual(event["project:an-example"], "Projects")
        self.assertEqual(event["asset:a-router"], "Assets")
        self.assertEqual(event["machine:lab-1"], "Machines")

    def test_what_is_not_offered_is_refused(self):
        page = self.client.get("/commands/calendar.entry.create/")
        key = page.context["form"].initial["__execution_key"]

        refused = self.client.post(
            "/commands/calendar.entry.create/",
            {"title": "Example", "starts_on": "2026-01-01", "about": "project:missing", "__execution_key": key},
        )

        self.assertEqual(refused.status_code, 400)
        self.assertIn("about", refused.context["form"].errors)

    def test_the_kind_offers_every_kind_hq_can_create(self):
        page = self.client.get("/commands/infrastructure.resource.create/")

        self.assertEqual(set(offered(page, "kind")), set(PROVIDERS))


class GuardedReferenceTests(TestCase):
    def setUp(self):
        Project.objects.create(name="An Example Account", slug="an-account")
        guarded = Target(
            "example.account",
            Project,
            Referable(key="slug", shows=("name",), role="account", requires="example.read"),
        )
        patched = mock.patch.object(references, "targets", lambda: {"example.account": guarded})
        patched.start()
        self.addCleanup(patched.stop)
        self.schema = capability_schema(capability_registry()["expense.create"])

    def test_a_kind_is_offered_only_to_who_may_name_it(self):
        reader = Principal("a reader", "web", frozenset({"read", "example.read"}))
        stranger = Principal("a stranger", "web", frozenset({"read"}))

        read = record_options(self.schema, principal=reader).get("paid_from")
        withheld = record_options(self.schema, principal=stranger).get("paid_from")

        self.assertEqual([option.value for option in read or ()], ["example.account:an-account"])
        self.assertEqual(withheld, ())

    def picker_on_each_form(self, principal: Principal, initial: dict[str, str] | None = None) -> tuple[bool, bool]:
        """Whether ``paid_from`` is on the command form and on the record's own form, for ``principal``."""

        spec = capability_registry()["expense.create"]
        command = command_form_class(spec, record_choices=record_options(self.schema, principal=principal))
        record = ExpenseForm(initial=initial)
        references.bind(record, principal=principal)
        return "paid_from" in command(initial=initial).fields, "paid_from" in record.fields

    def test_a_reference_that_offers_nothing_is_left_off_both_forms(self):
        reader = Principal("a reader", "web", frozenset({"read", "example.read"}))
        stranger = Principal("a stranger", "web", frozenset({"read"}))

        self.assertEqual(self.picker_on_each_form(reader), (True, True))
        self.assertEqual(self.picker_on_each_form(stranger), (False, False))

    def test_a_reference_that_offers_nothing_stays_while_it_holds_a_value(self):
        stranger = Principal("a stranger", "web", frozenset({"read"}))

        held = self.picker_on_each_form(stranger, initial={"paid_from": "example.account:an-account"})

        self.assertEqual(held, (True, True))
