"""Markup a page would otherwise write by hand is drawn by its one partial or
tag, so it reads the same everywhere and a change to it is made once."""

from django.test import SimpleTestCase

from hq.platform.application.markup_testing import elements, texts
from hq.platform.core.testing import render


class OutsideLinkTests(SimpleTestCase):
    def test_markup_links_out_the_way_a_name_does(self):
        html = render(
            '{% outside_link url=url css="chip" tone="good" tip="Success" %}<b>deploy</b>{% endoutside_link %}'
            "{% entity url|outside label=name %}",
            url="https://example.com/run",
            name="deploy",
        )

        chip, name = elements(html, "a")
        for link in (chip, name):
            self.assertEqual(link.attrs["target"], "_blank")
            self.assertEqual(set(link.attrs["rel"].split()), {"noopener", "noreferrer"})
        self.assertEqual(chip.classes, {"chip", "tone-good"})
        self.assertEqual(chip.attrs["data-tip"], "Success")
        self.assertEqual(name.attrs["title"], "Opens example.com")

    def test_markup_whose_address_is_not_a_web_page_is_drawn_unlinked(self):
        html = render("{% outside_link url=url %}<b>deploy</b>{% endoutside_link %}", url="javascript:alert(1)")

        self.assertEqual(elements(html, "a"), [])
        self.assertEqual(texts(html, "b"), ["deploy"])

    def test_a_count_of_nothing_is_still_shown(self):
        html = render("{% entity url|outside label=n %}", url="https://example.com/followers", n=0)

        self.assertEqual(texts(html, "a"), ["0"])

    def test_a_dialog_s_trigger_keeps_its_dialog(self):
        html = render('{% outside_link url="https://example.com/about" modal="about" %}About{% endoutside_link %}')

        (link,) = elements(html, "a")
        self.assertEqual(link.attrs["data-modal-open"], "about")
        self.assertEqual(link.attrs["aria-controls"], "modal-about")

    def test_a_page_outside_hq_opens_in_a_new_tab_without_a_way_back(self):
        html = render("{% entity url|outside label=name %}", url="https://example.com/a", name="example-repo")

        (link,) = elements(html, "a")
        self.assertEqual(link.attrs["href"], "https://example.com/a")
        self.assertEqual(link.attrs["target"], "_blank")
        self.assertEqual(set(link.attrs["rel"].split()), {"noopener", "noreferrer"})
        self.assertIn("external-link", link.classes)
        self.assertEqual(link.words, "example-repo")

    def test_an_address_that_is_not_a_web_page_is_not_a_link(self):
        for url in ("javascript:alert(1)", "/commit/abc", "", None):
            with self.subTest(url=url):
                html = render("{% entity url|outside label=name %}", url=url, name="abc")

                self.assertEqual(elements(html, "a"), [])
                self.assertIn("abc", html)


class RecordLinkTests(SimpleTestCase):
    """A record is linked by the page its model declares."""

    def setUp(self):
        from hq.domains.projects.models import Project

        self.project = Project(name="Example app", slug="example-app")

    def test_a_record_is_named_by_its_own_page(self):
        html = render("{% entity row|record label=row.name %}", row=self.project)

        (link,) = elements(html, "a")
        self.assertEqual(link.attrs["href"], self.project.get_absolute_url())
        self.assertEqual(link.attrs["data-entity"], "Project")
        self.assertEqual(link.words, "Example app")

    def test_a_section_of_the_page_is_its_anchor(self):
        html = render('{% entity row|record:"updates" label="2 open updates" %}', row=self.project)

        (link,) = elements(html, "a")
        self.assertEqual(link.attrs["href"], f"{self.project.get_absolute_url()}#updates")

    def test_no_record_draws_the_missing_mark(self):
        html = render("{% entity row|record %}", row=None)

        self.assertEqual(elements(html, "a"), [])
        self.assertEqual(html, render("{% empty_value %}"))

    def test_a_thing_with_no_page_of_its_own_is_refused(self):
        with self.assertRaises(AttributeError):
            render("{% entity row|record %}", row={"name": "example"})
