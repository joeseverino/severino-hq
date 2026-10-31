import re

from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase, tag
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.connection_kinds import CONNECTION_KINDS
from hq.domains.control_plane.models import ProviderConnection
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.provider_adapters import DECLARATIONS

from ..connection_setup import setups
from ..entity_links import connection_anchor
from ..page_reads import Remedy, need, pages_reading, readers, remedy, setup_url
from ..pages import PageAction, page_context
from .markup import all_elements, elements, hrefs, text as page_text, texts, with_class
from .source import template_files

PARTIAL = "partials/_empty_state.html"


def _templates():
    for name, path in template_files():
        yield name, path.read_text(encoding="utf-8")


class OneEmptyStateTests(SimpleTestCase):
    """An empty state is drawn by the one partial, so it reads the same everywhere."""

    def test_it_is_a_card_holding_one_paragraph(self):
        html = render_to_string(
            PARTIAL, {"message": "No containers yet.", "remedy": Remedy("Connect Portainer", "/connect/")}
        )

        (card,) = with_class(html, "empty")
        self.assertIn("card", card.classes)
        (line,) = texts(html, "p")
        self.assertTrue(line.startswith("No containers yet. Connect Portainer"))
        self.assertEqual(hrefs(html), ["/connect/"])

    def test_a_qualifying_sentence_stays_in_the_one_line(self):
        html = render_to_string(PARTIAL, {"message": "No problems found.", "why": "Example coverage."})

        self.assertEqual(texts(html, "p"), ["No problems found. Example coverage."])

    def test_a_remedy_with_no_page_is_said_not_linked(self):
        html = render_to_string(PARTIAL, {"message": "Nothing yet.", "remedy": Remedy("Ask the owner")})

        self.assertEqual(page_text(html), "Nothing yet. Ask the owner")
        self.assertEqual(elements(html, "a"), [])

    def test_a_list_without_reads_offers_its_own_lead_action(self):
        page = page_context(
            "Assets", actions=(PageAction("Export", "/export/"), PageAction("New asset", "/new/", primary=True))
        )["page"]

        self.assertEqual(page.remedy.label, "New asset")


@tag("source", "tree")
class EmptyStateTemplateTests(SimpleTestCase):
    """Templates draw an empty state only through the partial."""

    def test_only_the_partial_draws_an_empty_state(self):
        for name, text in _templates():
            if name == PARTIAL:
                continue
            with self.subTest(template=name):
                self.assertNotRegex(text, r'class="empty[ "]', "include partials/_empty_state.html instead")
                self.assertNotIn("{% block empty %}", text, "a list says what empty means with table_empty")

    def test_an_empty_state_is_one_line_and_one_remedy(self):
        include = re.compile(r'\{% include "partials/_empty_state\.html"(.*?)%\}', re.DOTALL)
        for name, text in _templates():
            for arguments in include.findall(text):
                with self.subTest(template=name, arguments=arguments):
                    self.assertNotIn("compact=", arguments, "the surface rule frames it, or not")
                    self.assertIn("message=", arguments)

    def test_no_page_names_an_identity_provider(self):
        comment = re.compile(r"\{% comment %\}.*?\{% endcomment %\}|\{#.*?#\}", re.DOTALL)
        for name, text in _templates():
            with self.subTest(template=name):
                self.assertNotIn("Pocket ID", comment.sub("", text), "say 'your sign-in'; HQ works with any")


class PageReadsTests(TestCase):
    def test_every_page_names_things_hq_declares(self):
        known = {definition.kind for definition in DECLARATIONS} | set(OBSERVATIONS) | set(CONNECTION_KINDS)
        views = pages_reading()

        self.assertTrue(views, "pages name what they read")
        for view in views:
            for name in view.page_reads:
                with self.subTest(view=view.__name__, name=name):
                    self.assertIn(name, known)
            with self.subTest(view=view.__name__):
                self.assertTrue(readers(view.page_reads), "a page that names what it reads is read by a connection")

    def test_the_reader_comes_from_the_kinds_own_declaration(self):
        self.assertEqual(readers(["portainer.container"]), ("portainer",))
        self.assertEqual(readers(["cloudflare.contact_submission"]), ("cloudflare_api",))
        # A keyless public lookup is no connection to make.
        self.assertEqual(readers(["registry.domain"]), ())

    def test_with_nothing_connected_it_offers_to_connect_the_reader(self):
        found = remedy(["portainer.container"])

        self.assertEqual(found, Remedy("Connect Portainer", setup_url("portainer")))
        self.assertTrue(found.url.startswith(reverse("control_plane:connections")))
        self.assertTrue(found.url.endswith("#setup-portainer"))

    def test_once_connected_it_names_that_connection_at_its_row(self):
        ProviderConnection.objects.create(
            connection_ref="example-portainer", provider="portainer", observed_at=timezone.now()
        )

        found = remedy(["portainer.container"])

        self.assertEqual(found.label, "Portainer connection")
        self.assertTrue(found.url.endswith(f"#{connection_anchor('example-portainer')}"))

    def test_an_empty_page_links_to_its_connection(self):
        self.client.force_login(_operator())

        response = self.client.get(reverse("control_plane:containers"))

        self.assertContains(response, f'href="{setup_url("portainer")}"'.replace("&", "&amp;"))
        self.assertContains(response, "Connect Portainer")

    def test_the_link_opens_that_service_on_the_connections_page(self):
        self.client.force_login(_operator())

        response = self.client.get(setup_url("portainer"))

        page = all_elements(response)
        (setup,) = [element for element in page if element.attrs.get("id") == "setup-portainer"]
        opened = [
            element
            for element in page
            if element.tag == "details" and any(ancestor is setup for ancestor in element.ancestors)
        ]
        self.assertIn("open", opened[0].attrs)


class SetupOrderTests(TestCase):
    def test_every_provider_says_what_connecting_it_gives(self):
        for provider, kind in CONNECTION_KINDS.items():
            with self.subTest(provider=provider):
                self.assertTrue(kind.gives.endswith("."), "one line in the owner's words")
                self.assertNotIn("\n", kind.gives)
                self.assertNotIn(kind.label, kind.gives, "what it gives, not its name again")

    def test_what_the_dashboard_reads_comes_first_then_what_most_pages_read(self):
        needed = need()
        order = [setup.provider for setup in setups()]
        keys = [(not needed.get(p, (False, 0))[0], -needed.get(p, (False, 0))[1]) for p in order]

        self.assertEqual(keys, sorted(keys))
        self.assertTrue(needed[order[0]][0], "the first is one the dashboard reads")


def _operator():
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_superuser("example", "example@example.com", "example-password")
