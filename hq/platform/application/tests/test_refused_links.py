"""A command that would refuse now is offered as unusable, saying why.

The example plugin's outbound work declares its refusals; a connection family
offers that work on its rows. The link to it, wherever it is drawn, carries
the refusal from the one declaration, and a press is still refused by the
server.
"""

from dataclasses import replace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.template.loader import render_to_string
from django.test import SimpleTestCase
from django.urls import reverse

from hq.domains.jobs.models import Job
from hq.platform.application.action_links import command_action, command_url
from hq.platform.application.connections import ConnectionAbility, ConnectionInstance, ConnectionSpec
from hq.platform.application.plugin_testing import offering
from hq.platform.application.plugins import clear_plugin_composition_cache
from hq.platform.application.workflow_contracts import ActionLink
from hq.platform.core.tests.estate_sizes import assert_cost_does_not_grow
from hq_sdk.testing import held_jobs
from tests.fixtures.example_hq_plugin import outbound as example

from ..entity_links import connection_anchor
from .markup import select, within
from .test_outbound_work import SCRIPT, ComposedExample

CLOSED = "The registry is closed for the night."


def _notes_spec(*notes: str) -> ConnectionSpec:
    """A connection per note, each offering the example's two pieces of work."""

    abilities = (
        ConnectionAbility("notes.lookup", "Look up", "Ask about this note.", capability=example.LOOKUP),
        ConnectionAbility("notes.refresh", "Refresh", "Ask about every note.", capability=example.REFRESH),
    )
    return ConnectionSpec(
        "example.notes",
        "Notes",
        "The registry's notes.",
        "notes.read",
        lambda: tuple(
            ConnectionInstance(note, note, "registry", "good", "Read", ability_names=("notes.lookup",))
            for note in notes
        ),
        abilities,
    )


DECLARED = example.work


def _closed_registry() -> tuple:
    """The example's work, with Refresh refused by a read of the database."""

    def refuse(subject):
        Job.objects.exists()
        return CLOSED

    return tuple(replace(work, refuse=refuse) if work.name == example.REFRESH else work for work in DECLARED())


def on_rows(page, *notes: str) -> list:
    """The elements on the rows of ``notes``, so what another installed
    extension offers on the same page is not counted."""

    return [element for note in notes for element in within(page, "id", connection_anchor(note))]


def refused_on(page, *notes: str) -> list:
    return [element for element in on_rows(page, *notes) if element.attrs.get("aria-disabled") == "true"]


class ConnectionsPage(ComposedExample):
    def connections(self, *notes: str, abilities: tuple[str, ...] = ("notes.lookup",)):
        spec = _notes_spec(*notes)
        provider = spec.instance_provider
        spec = replace(
            spec, instance_provider=lambda: tuple(replace(item, ability_names=abilities) for item in provider())
        )
        with offering(spec):
            return self.client.get(reverse("control_plane:connections"))

    def close_the_registry(self):
        patch = mock.patch.object(example, "work", _closed_registry)
        patch.start()
        self.addCleanup(patch.stop)
        clear_plugin_composition_cache()


class ARefusedCommandIsDrawnUnusableTests(ConnectionsPage):
    def test_a_row_offering_refused_work_says_why_and_cannot_be_pressed(self):
        page = self.connections(example.ARCHIVED, "first-note")

        (unusable,) = refused_on(page, example.ARCHIVED, "first-note")
        self.assertEqual((unusable.words, unusable.attrs["title"]), ("Look up", "An archived note is not looked up."))
        self.assertNotIn("href", unusable.attrs)
        self.assertNotIn("formaction", unusable.attrs)
        # The note that can be looked up is offered live, about its own row.
        (live,) = (
            element for element in on_rows(page, "first-note") if "href" in element.attrs and element.words == "Look up"
        )
        self.assertEqual(parse_qs(urlsplit(live.attrs["href"]).query)["target"], ["first-note"])

    def test_work_about_no_record_is_refused_on_every_row_that_offers_it(self):
        self.close_the_registry()

        page = self.connections("first-note", "second-note", abilities=("notes.refresh",))

        unusable = refused_on(page, "first-note", "second-note")
        self.assertEqual([(element.words, element.attrs["title"]) for element in unusable], [("Refresh", CLOSED)] * 2)

    def test_a_press_on_refused_work_is_still_refused_before_any_job(self):
        self.close_the_registry()

        with held_jobs() as held:
            response = self.client.post(reverse("jobs:ask", args=[example.REFRESH]), headers=SCRIPT)

        self.assertEqual((response.status_code, response.json()["note"]), (200, CLOSED))
        self.assertEqual((Job.objects.count(), held.waiting), (0, []))

    def test_the_page_asks_each_refusal_once_however_many_rows_offer_it(self):
        self.close_the_registry()

        assert_cost_does_not_grow(
            self,
            sizes=(1, 6),
            draw=lambda size: self.connections(*(f"note-{n}" for n in range(size)), abilities=("notes.refresh",)),
            rows=lambda page: len(refused_on(page, *(f"note-{n}" for n in range(6)))),
        )


class TheLinkCarriesTheRefusalTests(ComposedExample):
    def test_a_link_to_refused_work_carries_the_works_own_words(self):
        url = command_url(example.LOOKUP, example.ARCHIVED)

        refused = command_action("command", example.LOOKUP, url, target=example.ARCHIVED)
        live = command_action("command", example.LOOKUP, url, target="first-note")

        self.assertEqual(refused.refusal, "An archived note is not looked up.")
        self.assertEqual(live.refusal, "")

    def test_a_link_that_chooses_its_record_is_not_refused_for_having_none(self):
        link = command_action("command", example.LOOKUP, command_url(example.LOOKUP))

        self.assertEqual(link.refusal, "")


class TheRendererDrawsARefusalTests(SimpleTestCase):
    def render(self, *actions: ActionLink) -> str:
        return render_to_string("partials/_action_links.html", {"actions": actions})

    def test_a_refused_link_is_neither_a_link_nor_a_form(self):
        for method in ("GET", "POST"):
            with self.subTest(method):
                html = self.render(ActionLink("run", "Run", "remote_write", "/run/", method=method, refusal="Not now."))

                (refused,) = select(html, "aria-disabled", "true")
                self.assertEqual((refused.words, refused.attrs["title"]), ("Run", "Not now."))
                self.assertEqual(select(html, "href") + select(html, "formaction"), [])

    def test_a_link_that_would_run_is_live(self):
        html = self.render(ActionLink("run", "Run", "remote_write", "/run/"), ActionLink("go", "Go", "read", "/go/"))

        self.assertEqual(select(html, "aria-disabled"), [])
        self.assertEqual([element.attrs["href"] for element in select(html, "href")], ["/run/", "/go/"])
