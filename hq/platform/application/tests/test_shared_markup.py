"""Markup a page would otherwise write by hand is drawn by its one partial or
tag, so it reads the same everywhere and a change to it is made once."""

import re
from pathlib import Path

from django import forms
from django.conf import settings
from django.template import Context, Template, TemplateSyntaxError
from django.template.loader import render_to_string
from django.test import SimpleTestCase, tag

from .markup import elements, select, texts, with_class

TEMPLATES = Path(settings.BASE_DIR) / "templates"


def _templates():
    """Each template's name and source, the one walk every guard here reads."""

    for path in sorted(TEMPLATES.rglob("*.html")):
        yield path.relative_to(TEMPLATES).as_posix(), path.read_text(encoding="utf-8")


def _written_by_hand(pattern: str, partial: str) -> list[str]:
    """Every template but ``partial`` that writes ``pattern``."""

    return [name for name, text in _templates() if name != partial and re.search(pattern, text)]


def _render(source: str, **context) -> str:
    return Template(source).render(Context(context))


class _Refused(forms.Form):
    name = forms.CharField(required=False)

    def clean(self):
        raise forms.ValidationError("Those two cannot go together.")


class FormErrorsTests(SimpleTestCase):
    PARTIAL = "partials/_form_errors.html"

    def test_a_form_s_own_errors_are_announced_above_its_fields(self):
        form = _Refused(data={"name": "x"})
        form.is_valid()

        (notice,) = with_class(render_to_string(self.PARTIAL, {"form": form}), "msg-error")

        self.assertEqual(notice.attrs.get("role"), "alert")
        self.assertIn("Those two cannot go together.", notice.words)

    def test_a_form_without_them_draws_nothing(self):
        form = forms.Form(data={})
        form.is_valid()

        self.assertEqual(select(render_to_string(self.PARTIAL, {"form": form}), "role"), [])


class SectionHeadTests(SimpleTestCase):
    PARTIAL = "partials/_section_head.html"

    def test_the_heading_is_named_and_what_sits_beside_it_follows(self):
        html = _render(
            '{% section_head id="example-heading" %}Hosts <span class="count">{{ n }}</span>'
            '{% aside %}<a href="/example/">All</a>{% endsection_head %}',
            n=3,
        )

        (head,) = with_class(html, "section-head")
        (heading,) = elements(html, "h2")
        (link,) = elements(html, "a")
        self.assertEqual(heading.attrs["id"], "example-heading")
        self.assertEqual(heading.words, "Hosts 3")
        self.assertIn(head, heading.ancestors)
        self.assertNotIn(heading, link.ancestors)
        self.assertIn(head, link.ancestors)

    def test_a_lede_is_grouped_with_its_heading(self):
        html = _render("{% section_head lede=lede %}Load{% endsection_head %}", lede="Read a minute ago")

        (group,) = elements(html, "hgroup")
        (line,) = with_class(html, "section-lede")
        self.assertIn(group, line.ancestors)
        self.assertEqual(line.words, "Read a minute ago")

    def test_an_empty_lede_leaves_the_heading_alone(self):
        self.assertEqual(
            elements(_render("{% section_head lede=lede %}Load{% endsection_head %}", lede=""), "hgroup"), []
        )

    def test_what_the_page_writes_is_escaped_as_it_always_is(self):
        html = _render("{% section_head %}{{ name }}{% endsection_head %}", name="<b>x</b>")

        self.assertEqual(elements(html, "b"), [])

    def test_an_option_it_does_not_take_is_refused(self):
        for source in (
            '{% section_head class="x" %}A{% endsection_head %}',
            '{% section_head "A" %}A{% endsection_head %}',
        ):
            with self.subTest(source=source), self.assertRaises(TemplateSyntaxError):
                Template(source)


class BandFactTests(SimpleTestCase):
    PARTIAL = "partials/_band_fact.html"

    def test_a_fact_is_its_label_its_value_and_its_tone(self):
        html = _render(
            '{% band_fact icon="shield" label=label tone=tone %}<strong>{{ n }} open</strong>'
            "<small>example-repo</small>{% endband_fact %}",
            label="Security",
            tone="serious",
            n=2,
        )

        (fact,) = with_class(html, "band-fact")
        (named,) = with_class(html, "band-label")
        self.assertIn("tone-serious", fact.classes)
        self.assertEqual(named.words, "Security")
        self.assertEqual(len(with_class(html, "band-icon")), 1)
        self.assertEqual(texts(html, "strong"), ["2 open"])
        self.assertEqual(texts(html, "small"), ["example-repo"])

    def test_without_a_tone_or_an_icon_it_has_neither(self):
        html = _render('{% band_fact label="Running" tone=tone %}<strong>3</strong>{% endband_fact %}', tone="")

        (fact,) = with_class(html, "band-fact")
        self.assertEqual(fact.classes, {"band-fact"})
        self.assertEqual(with_class(html, "band-icon"), [])

    def test_a_page_cannot_decide_a_tone_in_the_template(self):
        # The fact's projection says its tone; the tag has no part for it.
        with self.assertRaises(TemplateSyntaxError):
            Template('{% band_fact label="A" %}<strong>1</strong>{% tone %}good{% endband_fact %}')

    def test_a_label_from_data_is_escaped(self):
        html = _render("{% band_fact label=label %}<strong>1</strong>{% endband_fact %}", label="<b>x</b>")

        self.assertEqual(elements(html, "b"), [])


class OutsideLinkTests(SimpleTestCase):
    PARTIAL = "partials/_outside_link.html"

    def test_markup_links_out_the_way_a_name_does(self):
        html = _render(
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
        html = _render("{% outside_link url=url %}<b>deploy</b>{% endoutside_link %}", url="javascript:alert(1)")

        self.assertEqual(elements(html, "a"), [])
        self.assertEqual(texts(html, "b"), ["deploy"])

    def test_a_count_of_nothing_is_still_shown(self):
        html = _render("{% entity url|outside label=n %}", url="https://example.com/followers", n=0)

        self.assertEqual(texts(html, "a"), ["0"])

    def test_a_dialog_s_trigger_keeps_its_dialog(self):
        html = _render('{% outside_link url="https://example.com/about" modal="about" %}About{% endoutside_link %}')

        (link,) = elements(html, "a")
        self.assertEqual(link.attrs["data-modal-open"], "about")
        self.assertEqual(link.attrs["aria-controls"], "modal-about")

    def test_a_page_outside_hq_opens_in_a_new_tab_without_a_way_back(self):
        html = _render("{% entity url|outside label=name %}", url="https://example.com/a", name="example-repo")

        (link,) = elements(html, "a")
        self.assertEqual(link.attrs["href"], "https://example.com/a")
        self.assertEqual(link.attrs["target"], "_blank")
        self.assertEqual(set(link.attrs["rel"].split()), {"noopener", "noreferrer"})
        self.assertIn("external-link", link.classes)
        self.assertEqual(link.words, "example-repo")

    def test_an_address_that_is_not_a_web_page_is_not_a_link(self):
        for url in ("javascript:alert(1)", "/commit/abc", "", None):
            with self.subTest(url=url):
                html = _render("{% entity url|outside label=name %}", url=url, name="abc")

                self.assertEqual(elements(html, "a"), [])
                self.assertIn("abc", html)


@tag("source", "tree")
class SharedMarkupTemplateTests(SimpleTestCase):
    """No template writes by hand what a shared partial or tag draws."""

    def test_only_the_partial_prints_a_form_s_own_errors(self):
        found = _written_by_hand(r"\.non_field_errors", FormErrorsTests.PARTIAL)
        self.assertEqual(found, [], f'include "{FormErrorsTests.PARTIAL}" instead')

    def test_only_the_tag_draws_a_section_head(self):
        found = _written_by_hand(r'class="section-head[" ]', SectionHeadTests.PARTIAL)
        self.assertEqual(found, [], "write {% section_head %} instead")

    def test_only_the_tag_draws_a_band_fact(self):
        found = _written_by_hand(r'class="band-fact[" {]', BandFactTests.PARTIAL)
        self.assertEqual(found, [], "write {% band_fact %} instead")

    def test_no_template_decides_a_fact_s_tone(self):
        # The tag refuses the part when a page renders; this reads every
        # template, rendered by a test or not.
        found = [
            f"{name}:{number}"
            for name, text in _templates()
            for number, line in enumerate(text.splitlines(), 1)
            if re.search(r"\{%\s*tone\s*%\}", line)
        ]
        self.assertFalse(found, "give the fact's projection a tone and write tone=<it> on {% band_fact %}")

    def test_only_the_outside_link_partial_writes_an_outside_link(self):
        # An anchor to a web address, or one that leaves in a new tab.
        outside = r'<a\b[^>]*(?:href="https?:|rel="noopener|target="_blank"|class="external-link)'
        found = _written_by_hand(outside, OutsideLinkTests.PARTIAL)
        self.assertEqual(found, [], "write {% entity url|outside label=name %} or {% outside_link %} instead")
