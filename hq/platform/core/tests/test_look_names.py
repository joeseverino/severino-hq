"""dev:look reaches a record's page of the estate it draws, by the route that
takes the record or by a template the page draws, a component catalog entry
as the catalog gate draws it, and the page a named POST's refusal answers; a
name it refuses is answered with names it takes, one a line, each as it is
typed; a rule it
refuses that is a check of the survey is answered with the gate test that
holds it."""

import os
from unittest import mock

from django.test import SimpleTestCase, TestCase
from django.urls import resolve, reverse

from hq.platform.core.tests import look
from hq.platform.core.tests.browser.estate import ask_as_production, estate_once, landings, owner_client
from hq.platform.core.tests.task import estate_name, estate_path, is_estate

ASKED = {"usage_pages": "", "usage_seed": "showcase"}


def _named(*asked):
    with mock.patch.dict(os.environ, ASKED, clear=True):
        return look._pages(asked)


def _offers(refused: AssertionError) -> list[str]:
    """The names a refusal offers: each line under a refused name that holds one, its first word."""

    return [line.split()[0] for line in str(refused).splitlines() if line.startswith("  ")]


class RefusedNamesTests(SimpleTestCase):
    def test_a_path_that_is_no_page_is_answered_with_names_that_reach_its_pages(self):
        # The gate's page by its name, and the estate's page of the route it meant.
        cases = (
            ("/dashboard/", {"dashboard", "estate"}),
            ("/control-plane/connections/", {"connections", estate_name(reverse("control_plane:connections"))}),
        )
        for asked, expected in cases:
            with self.subTest(asked=asked):
                with self.assertRaises(AssertionError) as refused:
                    _named(asked)
                offers = _offers(refused.exception)
                self.assertLessEqual(expected, set(offers))
                for offer in offers:
                    self.assertEqual(len(_named(offer)), 1, offer)


class CatalogNamesTests(SimpleTestCase):
    def test_an_entry_is_drawn_as_the_catalog_gate_draws_it(self):
        from hq.platform.core.tests.browser.catalog import CATALOG, CatalogLayoutTests

        names = [next(name for name in CATALOG if name.startswith("table/")), "part/notice-error"]
        self.assertEqual(_named(*names), names)
        gate = CatalogLayoutTests.render()
        self.assertEqual(look.catalog_pages(names), {name: gate[name] for name in names})

    def test_a_name_that_begins_entries_is_every_one_of_them(self):
        from hq.platform.core.tests.browser.catalog import CATALOG

        notices = [name for name in CATALOG if name.startswith("part/notice-")]
        self.assertTrue(notices)
        self.assertEqual(_named("part/notice"), notices)
        self.assertEqual(_named("table"), [name for name in CATALOG if name.startswith("table/")])

    def test_an_entry_that_is_none_is_refused_with_the_nearest(self):
        with self.assertRaises(AssertionError) as refused:
            _named("part/notice-eror")

        self.assertIn("part/notice-error", _offers(refused.exception))


class PostNamesTests(TestCase):
    def test_every_named_post_is_refused_on_the_seed_as_it_says(self):
        from hq.platform.core.tests.look_posts import post_names, posted

        names = post_names()
        self.assertEqual(_named(*names), names)
        with owner_client() as client:
            for name in names:
                with self.subTest(post=name):
                    sent = posted(name)
                    response = sent.send(client)
                    self.assertEqual(response.status_code, sent.status)
                    self.assertTrue(response.context["form"].errors)

    def test_a_post_that_is_none_is_refused_with_the_nearest(self):
        with self.assertRaises(AssertionError) as refused:
            _named("post:project-create-blnk")

        self.assertIn("post:project-create-blank", _offers(refused.exception))


class RefusedRuleTests(SimpleTestCase):
    def test_a_check_of_the_survey_is_answered_with_the_gate_test_that_holds_it(self):
        # A palette check is held in the dark palette too, by its own test.
        cases = (("contrast", "every_palette_check_holds"), ("sideways", "every_layout_check_holds"))
        for check, holder in cases:
            with self.subTest(check=check):
                said = look.no_rule(check)
                self.assertIn(f"--rule {holder}", said)
                self.assertIn(holder, look._rules())

    def test_a_name_that_is_no_check_names_no_gate_test(self):
        said = look.no_rule("example-no-such-check")

        self.assertNotIn("--rule every_", said)


class EveryPageTests(TestCase):
    def test_all_holds_the_api_reference_light_and_dark(self):
        pages = _named("all")

        self.assertLessEqual(set(look.REFERENCE_PAGES), set(pages))

    def test_all_reads_every_page_the_estate_gate_walks(self):
        # Each as the page it lands on, as the gate lists them and dev:look draws them.
        asked = [estate_path(page) for page in _named("all") if is_estate(page)]
        walked, _ = estate_once()
        with owner_client() as client:
            read = landings(lambda address: ask_as_production(client, address), asked)

        self.assertEqual(sorted(read), sorted(walked))


class GuessTests(SimpleTestCase):
    def test_a_page_behind_the_session_is_asked_as_a_guess_and_a_sessionless_route_as_made(self):
        # The reference's document answers without a session, where a guess is refused whole.
        self.assertTrue(look._guessable(reverse("dashboard")))
        self.assertFalse(look._guessable(reverse("hq_api:openapi")))


class RecordPagesTests(TestCase):
    def drawn(self, *pages):
        """Each estate page of ``pages`` as its owner reaches it: its route and the templates it draws."""

        said = []
        with owner_client() as client:
            for page in pages:
                self.assertTrue(is_estate(page), page)
                response = ask_as_production(client, estate_path(page))
                self.assertEqual(response.status_code, 200, page)
                said.append((resolve(estate_path(page)).view_name, {template.name for template in response.templates}))
        return said

    def test_a_route_that_takes_a_record_reaches_a_record_of_the_estate(self):
        [detail] = _named("control_plane:detail")

        [(route, templates)] = self.drawn(detail)
        self.assertEqual(route, "control_plane:detail")
        self.assertIn("control_plane/resource_detail.html", templates)

    def test_a_template_named_whole_or_by_its_file_reaches_the_estate_page_that_draws_it(self):
        [container] = _named("template:control_plane/_container_detail.html", "template:_container_detail.html")

        [(_, templates)] = self.drawn(container)
        self.assertIn("control_plane/_container_detail.html", templates)

    def test_a_record_the_estate_does_not_hold_is_refused_with_records_it_does(self):
        with self.assertRaises(AssertionError) as refused:
            _named("estate/infrastructure/example-no-such-record")
        offers = _offers(refused.exception)

        self.assertTrue(offers)
        reached = _named(*offers)
        self.assertTrue(reached)
        for route, _ in self.drawn(*reached):
            self.assertEqual(route, "control_plane:detail")
