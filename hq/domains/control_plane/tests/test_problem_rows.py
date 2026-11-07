"""Where a thing's page draws a problem that is not urgent: under the part it
is about, in that part's card, or in the problems' place above the page;
never loose between the page's cards."""

from django.test import TestCase
from django.urls import reverse

from hq.platform.application.problems import problem_counts
from hq.platform.application.projection import projection_scope
from hq.platform.application.security import cli_principal
from hq.platform.application.tests.markup import Element, all_elements
from hq.platform.core import seeds
from hq.platform.core.example_estate.names import MACHINE
from hq.platform.core.example_estate.testing import throwaway_media

# The rule whose finding the example machine raises about one of its containers.
_ABOUT_A_CONTAINER = "container-image-unpinned"


def _in_card(element: Element) -> Element | None:
    return next((ancestor for ancestor in reversed(element.ancestors) if "card" in ancestor.classes), None)


class ProblemRowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("standard") as owner:
            cls.owner = owner

    def setUp(self):
        self.client.force_login(self.owner)

    def test_a_row_about_a_container_sits_in_the_machines_containers_card(self):
        page = all_elements(self.client.get(reverse("control_plane:machine", kwargs={"name": MACHINE})))

        marks = [element for element in page if _ABOUT_A_CONTAINER in element.attrs.get("data-origin", "")]
        self.assertTrue(marks)
        for mark in marks:
            row = next(ancestor for ancestor in mark.ancestors if "data-attention-item" in ancestor.attrs)
            self.assertIsNotNone(_in_card(row))
            section = next(ancestor for ancestor in reversed(row.ancestors) if "aria-labelledby" in ancestor.attrs)
            self.assertEqual(section.attrs["aria-labelledby"], "containers-heading")

    def test_no_row_on_a_things_page_sits_loose_between_its_cards(self):
        with projection_scope():
            pages = sorted(problem_counts(cli_principal()))
        self.assertIn(reverse("control_plane:machine", kwargs={"name": MACHINE}), pages)

        for url in pages:
            with self.subTest(page=url):
                page = all_elements(self.client.get(url))
                cards = [at for at, element in enumerate(page) if "card" in element.classes]
                first = min((at for at in cards if page[at].attrs.get("id") != "problems-here"), default=len(page))
                for at, element in enumerate(page):
                    if "data-attention-item" in element.attrs and _in_card(element) is None:
                        self.assertLess(at, first, f"{element.words[:80]} sits outside a card, below one")
