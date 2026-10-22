"""A record names a thing in HQ one way, and the thing lists what names it."""

from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, models
from django.test import TestCase
from django.test.utils import CaptureQueriesContext, isolate_apps
from django.urls import reverse
from django.utils import timezone

from hq.domains.assets.models import Asset
from hq.domains.calendars.models import Entry
from hq.domains.control_plane.models import ProviderConnection
from hq.domains.expenses.models import Expense
from hq.domains.projects.models import Project

from .. import references
from ..assets import AssetCommand, save_asset
from ..calendar_entries import EntryCommand, save_entry
from ..references import Referable, Reference, ReferenceField, Target
from ..security import Principal, cli_principal, internal_principal
from .markup import all_elements, elements, hrefs, links, select, text, texts


def one(items):
    """The only item of a sequence that holds exactly one."""

    items = list(items)
    assert len(items) == 1, items
    return items[0]


def a_machine(name: str = "lab-1") -> None:
    ProviderConnection.objects.create(
        connection_ref="a-portainer",
        controller_id="a-controller",
        provider="portainer",
        endpoint="https://portainer.example.com",
        reaches=[name],
        reachable=True,
        probed=True,
        observed_at=timezone.now(),
    )


class Picker(dict[str, list[tuple[str, str]]]):
    """A reference picker's choices by group, each as its value and the words shown."""

    @property
    def all(self) -> list[tuple[str, str]]:
        return [choice for choices in self.values() for choice in choices]


def picker(page) -> Picker:
    found = Picker()
    for option in elements(page, "option"):
        group = next((outer.attrs.get("label", "") for outer in option.ancestors if outer.tag == "optgroup"), "")
        found.setdefault(group, []).append((option.attrs.get("value", ""), option.words))
    return found


def fact(page, term: str) -> tuple[str, list]:
    """What a definition list says for ``term``, and the links in it."""

    page_elements = all_elements(page)
    for index, element in enumerate(page_elements):
        if element.tag == "dt" and element.words == term:
            definition = next(later for later in page_elements[index + 1 :] if later.tag == "dd")
            linked = [
                inner
                for inner in page_elements
                if inner.tag == "a" and any(outer is definition for outer in inner.ancestors)
            ]
            return definition.words, linked
    raise AssertionError(f"No {term!r} on the page.")


def an_expense(**fields) -> Expense:
    return Expense.objects.create(
        date=date(2026, 3, 1), vendor="Example Registrar", item="Renewal", total_cost=Decimal("12.00"), **fields
    )


class ReferenceTextTests(TestCase):
    def test_a_reference_is_a_kind_and_an_identity(self):
        self.assertEqual(Reference.parse("machine:lab-1"), Reference("machine", "lab-1"))
        self.assertEqual(str(Reference("zone", "example.com")), "zone:example.com")

    def test_an_identity_keeps_its_own_colons(self):
        self.assertEqual(Reference.parse("resource:tls:example").identity, "tls:example")

    def test_text_with_no_kind_or_no_identity_is_no_reference(self):
        for value in ("", "lab-1", "machine:", ":lab-1", None):
            self.assertIsNone(Reference.parse(value))

    def test_a_person_names_a_kind_by_its_noun(self):
        principal = internal_principal("test")
        self.assertEqual(references.authored("domain:example.com", principal=principal), "zone:example.com")
        self.assertEqual(references.authored("asset:a-router", principal=principal), "asset:a-router")

    def test_text_that_reads_as_no_reference_is_kept_as_written(self):
        self.assertEqual(references.authored(" the router ", principal=internal_principal("test")), "the router")


class ReferenceFieldTests(TestCase):
    def test_a_migration_sees_a_plain_text_column(self):
        _name, path, _args, kwargs = Asset._meta.get_field("infrastructure").deconstruct()

        self.assertEqual(path, "django.db.models.CharField")
        self.assertFalse({"kinds", "role", "but", "heading", "shows", "note"} & set(kwargs))

    def test_every_reference_keeps_a_name_beside_it_and_says_its_heading(self):
        for field in references.reference_fields():
            with self.subTest(field=str(field)):
                self.assertEqual(field.check(), [])
                self.assertTrue(field.heading)

    @isolate_apps("hq.domains.assets")
    def test_a_reference_without_its_name_column_fails_the_system_check(self):
        class Lonely(models.Model):  # noqa: DJ008 - stored state, never shown by name
            about = ReferenceField()

            class Meta:
                app_label = "assets"

        self.assertEqual(
            [error.id for error in Lonely._meta.get_field("about").check()],
            ["hq.references.E001", "hq.references.E002"],
        )

    def test_saving_through_the_model_keeps_the_name_of_what_it_names(self):
        Project.objects.create(name="An Example Project", slug="an-example")
        entry = Entry(title="Review", starts_on=date(2026, 3, 1), about="project:an-example")

        entry.full_clean()

        self.assertEqual(entry.about_name, "An Example Project")

    def test_a_new_reference_that_names_nothing_is_refused(self):
        entry = Entry(title="Review", starts_on=date(2026, 3, 1), about="project:missing")

        with self.assertRaises(ValidationError) as raised:
            entry.full_clean()

        self.assertIn("about", raised.exception.message_dict)

    def test_a_reference_of_a_kind_the_column_does_not_take_is_refused(self):
        Project.objects.create(name="An Example Project", slug="an-example")
        asset = Asset(item_name="A router", infrastructure="project:an-example")

        with self.assertRaises(ValidationError):
            asset.full_clean()

    def test_a_stored_reference_survives_its_thing_going(self):
        project = Project.objects.create(name="An Example Project", slug="an-example")
        entry = Entry(title="Review", starts_on=date(2026, 3, 1), about="project:an-example")
        entry.full_clean()
        entry.save()
        project.delete()

        entry.title = "Review again"
        entry.full_clean()

        self.assertEqual((entry.about, entry.about_name), ("project:an-example", "An Example Project"))

    def test_clearing_a_reference_clears_its_name(self):
        entry = Entry(title="Review", starts_on=date(2026, 3, 1), about="", about_name="An Example Project")

        entry.full_clean()
        entry.save()

        self.assertEqual(Entry.objects.get().about_name, "")

    def test_a_command_writes_a_reference_as_the_form_does(self):
        a_machine()
        result = save_asset(
            AssetCommand(item_name="A server", infrastructure="machine:lab-1"), principal=cli_principal()
        )

        asset = Asset.objects.get(slug=result["asset"]["slug"])
        self.assertEqual((asset.infrastructure, asset.infrastructure_name), ("machine:lab-1", "lab-1"))
        self.assertEqual(result["asset"]["infrastructure"], "machine:lab-1")
        with self.assertRaises(ValidationError):
            save_entry(
                EntryCommand(title="Review", starts_on=date(2026, 3, 1), about="machine:missing"),
                principal=cli_principal(),
            )


class ResolveTests(TestCase):
    def setUp(self):
        self.principal = internal_principal("test")

    def test_a_record_resolves_to_its_name_and_its_page(self):
        Asset.objects.create(item_name="A router", slug="a-router")

        link = references.resolve("asset:a-router", principal=self.principal)

        self.assertEqual((link.label, link.url), ("A router", reverse("assets:detail", args=["a-router"])))
        self.assertEqual(link.kind_label, "Asset")

    def test_a_machine_resolves_through_the_topology(self):
        a_machine()

        link = references.resolve("machine:lab-1", principal=self.principal)

        self.assertEqual(link.url, reverse("control_plane:machine", kwargs={"name": "lab-1"}))

    def test_a_reference_that_names_nothing_is_its_stored_name_and_no_page(self):
        link = references.resolve("machine:lab-9", "The old server", principal=self.principal)

        self.assertEqual((link.label, link.url, link.kind_label), ("The old server", "", "Machine"))

    def test_text_that_is_no_reference_is_shown_as_written(self):
        link = references.resolve("the router", principal=self.principal)

        self.assertEqual((link.label, link.url), ("the router", ""))

    def test_no_reference_is_nothing(self):
        self.assertIsNone(references.resolve("", principal=self.principal))

    def test_an_identity_that_cannot_be_a_key_names_nothing(self):
        link = references.resolve("expense:not-a-number", principal=self.principal)

        self.assertEqual(link.url, "")

    def test_a_page_of_references_costs_what_one_reference_does(self):
        Asset.objects.create(item_name="A router", slug="a-router")
        Project.objects.create(name="An Example Project", slug="an-example")
        expense = an_expense()

        with CaptureQueriesContext(connection) as one:
            references.resolve_many([("asset:a-router", "")], principal=self.principal)
        with CaptureQueriesContext(connection) as queries:
            links = references.resolve_many(
                [("asset:a-router", ""), ("project:an-example", ""), (f"expense:{expense.pk}", ""), ("", "")],
                principal=self.principal,
            )

        self.assertEqual(len(queries), len(one))
        self.assertEqual(
            [link.label if link else None for link in links],
            ["A router", "An Example Project", "Example Registrar · Renewal", None],
        )


class GuardedKindTests(TestCase):
    """A model may ask a capability of whoever its rows are named to."""

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
        self.reader = Principal("a reader", "web", frozenset({"read", "example.read"}))
        self.stranger = Principal("a stranger", "web", frozenset({"read"}))
        self.field = Expense._meta.get_field("paid_from")

    def test_a_viewer_without_the_capability_is_told_nothing(self):
        self.assertIsNone(
            references.resolve("example.account:an-account", "An Example Account", principal=self.stranger)
        )
        self.assertEqual(
            references.resolve("example.account:an-account", principal=self.reader).label, "An Example Account"
        )

    def test_the_picker_offers_a_role_only_to_who_may_read_it(self):
        offered = references.choices(self.field, principal=self.reader)
        withheld = references.choices(self.field, principal=self.stranger)

        self.assertEqual(offered[1], ("Projects", [("example.account:an-account", "An Example Account")]))
        self.assertEqual(withheld, [("", "Nothing")])

    def test_a_role_takes_no_other_kind(self):
        self.assertTrue(self.field.accepts("example.account"))
        self.assertFalse(self.field.accepts("machine"))


class PickerTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("operator", "operator@example.com", "a-password")
        self.client.force_login(self.user)

    def test_an_event_form_offers_what_hq_has_pages_for_grouped_by_what_each_is(self):
        a_machine()
        Project.objects.create(name="An Example Project", slug="an-example")
        Asset.objects.create(item_name="A router", slug="a-router")
        an_expense()

        response = self.client.get(reverse("calendar:entry_new"))

        offered = picker(response)
        self.assertIn(("machine:lab-1", "lab-1"), offered["Machines"])
        self.assertIn(("project:an-example", "An Example Project"), offered.all)
        self.assertIn(("asset:a-router", "A router"), offered.all)
        # A kind with a row per purchase is never a list to scroll.
        self.assertNotContains(response, 'value="expense:')

    def test_the_form_reads_every_record_it_offers_in_one_statement(self):
        Project.objects.create(name="An Example Project", slug="an-example")
        Asset.objects.create(item_name="A router", slug="a-router")
        field = Entry._meta.get_field("about")
        principal = internal_principal("test")
        references.choices(field, principal=principal)

        with CaptureQueriesContext(connection) as queries:
            references.choices(field, principal=principal)

        self.assertEqual(len([query for query in queries if "projects_project" in query["sql"]]), 1)
        self.assertEqual(len([query for query in queries if "assets_asset" in query["sql"]]), 1)

    def test_a_picker_with_nothing_to_offer_is_left_off_the_form(self):
        response = self.client.get(reverse("expenses:create"))

        self.assertNotIn("paid_from", response.context["form"].fields)

    def test_an_asset_takes_a_machine_a_domain_or_a_certificate_and_nothing_else(self):
        a_machine()
        Project.objects.create(name="An Example Project", slug="an-example")

        response = self.client.get(reverse("assets:create"))

        offered = picker(response)
        self.assertIn(("machine:lab-1", "lab-1"), offered.all)
        self.assertNotIn("project:an-example", [value for value, _ in offered.all])

    def test_choosing_one_saves_it_with_its_name(self):
        a_machine()

        response = self.client.post(
            reverse("calendar:entry_new"),
            {"title": "Replace the fan", "starts_on": "2026-03-01", "interval": "1", "about": "machine:lab-1"},
        )

        self.assertEqual(response.status_code, 302)
        entry = Entry.objects.get()
        self.assertEqual((entry.about, entry.about_name), ("machine:lab-1", "lab-1"))

    def test_what_is_not_offered_is_refused(self):
        a_machine()

        response = self.client.post(
            reverse("calendar:entry_new"),
            {"title": "Replace the fan", "starts_on": "2026-03-01", "interval": "1", "about": "machine:lab-9"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("about", response.context["form"].errors)

    def test_a_reference_whose_thing_has_gone_stays_on_the_form(self):
        entry = Entry.objects.create(
            title="Replace the fan", starts_on=date(2026, 3, 1), about="machine:lab-9", about_name="lab-9"
        )

        response = self.client.get(reverse("calendar:entry_edit", args=[entry.uid]))

        self.assertEqual(picker(response)["No longer in HQ"], [("machine:lab-9", "lab-9")])
        self.assertIn(
            "machine:lab-9",
            [option.attrs["value"] for option in elements(response, "option") if "selected" in option.attrs],
        )


class ReferencedByTests(TestCase):
    def setUp(self):
        self.principal = internal_principal("test")
        self.asset = Asset.objects.create(
            item_name="A server", slug="a-server", purchase_date=date(2025, 1, 5), infrastructure="machine:lab-1"
        )

    def test_a_thing_lists_what_names_it_under_each_reference_s_heading(self):
        Entry.objects.create(title="Replace the fan", starts_on=date(2026, 3, 1), about="machine:lab-1")
        an_expense(about="machine:lab-1")

        found = {
            group.heading: group.items
            for group in references.referenced_by("machine", "lab-1", principal=self.principal)
        }

        self.assertEqual(set(found), {"Assets", "On the calendar", "Expenses"})
        (asset,) = found["Assets"]
        self.assertEqual((asset.link.label, asset.link.url), ("A server", self.asset.get_absolute_url()))
        self.assertEqual(asset.note, "Bought Jan\xa05,\xa02025")
        self.assertEqual(found["On the calendar"][0].note, "Mar\xa01")
        self.assertEqual(found["Expenses"][0].note, "Mar\xa01 · $12.00")

    def test_a_row_that_names_a_thing_is_listed_by_the_name_it_has_everywhere(self):
        expense = an_expense(about="machine:lab-1")

        found = {
            group.heading: group.items
            for group in references.referenced_by("machine", "lab-1", principal=self.principal)
        }

        # Its date is its note, written as HQ writes a date; never part of its name.
        self.assertEqual(found["Expenses"][0].link.label, expense.label)

    def test_it_costs_the_same_however_many_models_hold_a_reference(self):
        Entry.objects.create(title="Replace the fan", starts_on=date(2026, 3, 1), about="machine:lab-1")
        an_expense(about="machine:lab-1")
        every = tuple(field for field in references.reference_fields() if field.accepts("machine"))
        self.assertGreater(len(every), 2)

        def cost(fields):
            with (
                mock.patch("hq.platform.application.references.reference_fields", return_value=fields),
                CaptureQueriesContext(connection) as queries,
            ):
                references.referenced_by("machine", "lab-1", principal=self.principal)
            return len(queries)

        self.assertEqual(cost(every), cost(every[:1]))

    def test_a_repeating_event_says_how_it_repeats(self):
        Entry.objects.create(
            title="Dust the rack", starts_on=date(2026, 3, 1), repeat="monthly", about="asset:a-server"
        )

        (group,) = references.referenced_by_row(self.asset, principal=self.principal)

        self.assertEqual((group.heading, group.items[0].note), ("On the calendar", "Every month"))

    def test_an_event_links_to_its_own_day(self):
        entry = Entry.objects.create(title="Replace the fan", starts_on=date(2026, 3, 1), about="asset:a-server")

        (group,) = references.referenced_by_row(self.asset, principal=self.principal)

        self.assertEqual(group.items[0].link.url, entry.get_absolute_url())

    def test_nothing_names_it_and_nothing_is_listed(self):
        self.assertEqual(references.referenced_by("machine", "lab-2", principal=self.principal), ())

    def test_a_model_that_cannot_be_referred_to_says_so(self):
        entry = Entry.objects.create(title="Replace the fan", starts_on=date(2026, 3, 1))

        with self.assertRaises(LookupError):
            references.referenced_by_row(entry, principal=self.principal)


class DanglingTests(TestCase):
    def test_a_row_whose_reference_names_nothing_is_one_item_for_the_queue(self):
        asset = Asset.objects.create(
            item_name="A server", slug="a-server", infrastructure="machine:lab-9", infrastructure_name="lab-9"
        )
        Asset.objects.create(item_name="A router", slug="a-router")

        item = one(references.dangling(Asset))

        self.assertEqual(item.title, "A server links to lab-9, which HQ does not have")
        self.assertEqual(item.url, asset.get_absolute_url())
        self.assertEqual(item.key, f"reference:assets.asset:{asset.pk}:infrastructure")

    def test_a_reference_that_names_something_is_not_reported(self):
        a_machine()
        Asset.objects.create(item_name="A server", slug="a-server", infrastructure="machine:lab-1")

        self.assertEqual(references.dangling(Asset), ())

    def test_the_whole_estate_is_three_statements(self):
        for number in range(6):
            Asset.objects.create(item_name=f"Server {number}", infrastructure=f"machine:lab-{number}")
            Entry.objects.create(title="Check", starts_on=date(2026, 3, 1), about=f"asset:gone-{number}")

        with CaptureQueriesContext(connection) as queries:
            found = references.dangling(Asset, Entry)

        self.assertEqual(len(found), 12)
        # What is held, which of it names a record, and the rows that name nothing.
        self.assertEqual(len([query for query in queries if "assets_asset" in query["sql"]]), 3)

    def test_the_asset_and_expense_sections_report_their_own(self):
        from ..attention import assets, expenses

        Asset.objects.create(
            item_name="A server",
            purchase_date=date(2025, 1, 5),
            total_cost=Decimal("1.00"),
            infrastructure="machine:lab-9",
            infrastructure_name="lab-9",
        )
        an_expense(about="machine:lab-9", about_name="lab-9")

        self.assertIn("A server links to lab-9, which HQ does not have", [item.title for item in assets()])
        self.assertIn(
            "Example Registrar · Renewal links to lab-9, which HQ does not have",
            [item.title for item in expenses()],
        )


class PageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser("operator", "operator@example.com", "a-password")
        self.client.force_login(self.user)
        a_machine()
        self.asset = Asset.objects.create(
            item_name="A server", slug="a-server", infrastructure="machine:lab-1", infrastructure_name="lab-1"
        )

    def test_an_asset_page_links_what_it_is_and_lists_what_names_it(self):
        entry = Entry.objects.create(title="Replace the fan", starts_on=date(2026, 3, 1), about="asset:a-server")

        response = self.client.get(self.asset.get_absolute_url())

        machine = reverse("control_plane:machine", kwargs={"name": "lab-1"})
        said, linked = fact(response, "This is")
        self.assertEqual(said, "Machine lab-1")
        self.assertEqual(
            [(link.words, link.attrs["href"], link.attrs["data-entity"]) for link in linked],
            [("lab-1", machine, "Machine")],
        )
        self.assertIn("On the calendar", text(response))
        self.assertIn(entry.get_absolute_url(), hrefs(response))

    def test_an_asset_whose_machine_has_gone_shows_its_name_as_text(self):
        Asset.objects.filter(pk=self.asset.pk).update(infrastructure="machine:lab-9", infrastructure_name="lab-9")

        response = self.client.get(self.asset.get_absolute_url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(fact(response, "This is"), ("Machine lab-9", []))
        self.assertIn("lab-9", [named.words for named in select(response, "data-entity", "Machine")])

    def test_an_expense_page_links_what_it_was_for(self):
        expense = an_expense(about="machine:lab-1", about_name="lab-1")

        response = self.client.get(expense.get_absolute_url())

        _, linked = fact(response, "For")
        self.assertEqual([(link.words, link.attrs["data-entity"]) for link in linked], [("lab-1", "Machine")])

    def test_an_event_says_what_it_is_about(self):
        entry = Entry.objects.create(
            title="Replace the fan", starts_on=date(2026, 3, 1), about="asset:a-server", about_name="A server"
        )

        response = self.client.get(entry.get_absolute_url())

        self.assertIn("About asset A server", text(response))
        self.assertIn(("A server", self.asset.get_absolute_url()), links(response))

    def test_the_tag_lists_what_names_a_kind_and_identity(self):
        from django.template import Context, Template
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.user = self.user
        drawn = Template('{% referenced_by "machine" "lab-1" cards=True %}').render(Context({"request": request}))

        self.assertIn("Assets 1", texts(drawn, "h2"))
        self.assertIn(self.asset.get_absolute_url(), hrefs(drawn))

    def test_pages_keep_their_query_budgets(self):
        Entry.objects.create(title="Replace the fan", starts_on=date(2026, 3, 1), about="asset:a-server")
        expense = an_expense(about="machine:lab-1", about_name="lab-1", related_asset=self.asset)
        # One statement for what names the record, and the topology's two
        # reads from its store where the record names a machine.
        for url, budget in ((self.asset.get_absolute_url(), 13), (expense.get_absolute_url(), 9)):
            self.client.get(url)
            with self.subTest(url=url), CaptureQueriesContext(connection) as queries:
                self.client.get(url)
            self.assertLessEqual(
                len(queries), budget, [query["sql"][:90] + " ... " + query["sql"][-150:] for query in queries]
            )
