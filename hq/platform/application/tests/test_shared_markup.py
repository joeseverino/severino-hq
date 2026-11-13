"""Markup a page would otherwise write by hand is drawn by its one partial or
tag, so it reads the same everywhere and a change to it is made once."""

import re

from django import forms
from django.template import Context, Template, TemplateSyntaxError
from django.template.loader import render_to_string
from django.test import SimpleTestCase, tag

from .markup import elements, select, texts, with_class
from .source import template_files


def _templates():
    """Each template's name and source, the one walk every guard here reads."""

    for name, path in template_files():
        yield name, path.read_text(encoding="utf-8")


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

        (notice,) = with_class(render_to_string(self.PARTIAL, {"form": form}), "notice-error")

        self.assertEqual(notice.attrs.get("role"), "alert")
        self.assertIn("Those two cannot go together.", notice.words)

    def test_a_form_without_them_draws_nothing(self):
        form = forms.Form(data={})
        form.is_valid()

        self.assertEqual(select(render_to_string(self.PARTIAL, {"form": form}), "role"), [])


class SectionHeadTests(SimpleTestCase):
    """The head ``{% section %}`` draws (partials/_section_head.html)."""

    PARTIAL = "partials/_section_head.html"

    def test_what_sits_beside_the_heading_is_one_box_of_the_head(self):
        """A count, a dot and a link beside the heading wrap as one line of
        text, never as items of the head's row a dot is stranded between."""

        aside = '<span>6 active</span>{% dot %}<a href="/example/">All</a>'
        for source, box in (
            (f'{{% section "Projects" %}}<p>Body</p>{{% aside %}}{aside}{{% endsection %}}', "div"),
            (f'{{% section "Projects" fold=True %}}<p>Body</p>{{% aside %}}{aside}{{% endsection %}}', "span"),
        ):
            with self.subTest(box=box):
                html = _render(source)
                (head,) = with_class(html, "section-head")
                beside = [
                    el
                    for el in elements(html, "span") + elements(html, "a") + elements(html, "div")
                    if el.ancestors and el.ancestors[-1] == head
                ]
                self.assertEqual([(el.tag, el.classes) for el in beside], [(box, frozenset({"section-aside"}))])
                (aside,) = beside
                self.assertEqual({el.words for el in elements(html, "a")}, {"All"})
                self.assertIn(aside, elements(html, "a")[0].ancestors)

    def test_with_nothing_beside_the_heading_the_head_draws_no_aside(self):
        html = _render('{% section "Load" %}<p>Body</p>{% aside %} {% endsection %}')

        self.assertEqual(with_class(html, "section-aside"), [])

    def test_a_lede_is_grouped_with_its_heading(self):
        html = _render('{% section "Load" lede=lede %}<p>Body</p>{% endsection %}', lede="Read a minute ago")

        (group,) = elements(html, "hgroup")
        (line,) = with_class(html, "section-lede")
        self.assertIn(group, line.ancestors)
        self.assertEqual(line.words, "Read a minute ago")

    def test_an_empty_lede_leaves_the_heading_alone(self):
        html = _render('{% section "Load" lede=lede %}<p>Body</p>{% endsection %}', lede="")

        self.assertEqual(elements(html, "hgroup"), [])


class CardTests(SimpleTestCase):
    PARTIAL = "partials/_card.html"

    def test_a_card_frames_what_it_holds_under_its_heading(self):
        html = _render(
            '{% card css="example-card" heading="example-heading" %}'
            '<span id="example-heading">Hosts</span><p>{{ n }} up</p>{% endcard %}',
            n=3,
        )

        (card,) = with_class(html, "card")
        (line,) = elements(html, "p")
        self.assertEqual(card.tag, "section")
        self.assertEqual(card.classes, {"card", "example-card"})
        self.assertEqual(card.attrs["aria-labelledby"], "example-heading")
        self.assertIn(card, line.ancestors)
        self.assertEqual(line.words, "3 up")

    def test_a_card_is_the_element_it_is_asked_to_be(self):
        html = _render('{% card element="details" %}<summary>More</summary>{% endcard %}')

        (card,) = with_class(html, "card")
        self.assertEqual(card.tag, "details")

    def test_a_card_is_never_an_inline_or_unknown_element(self):
        for element in ("span", "p", "script", ""):
            with self.subTest(element=element), self.assertRaises(TemplateSyntaxError):
                _render("{% card element=element %}A{% endcard %}", element=element)

    def test_a_data_option_is_its_attribute_bare_when_true_and_absent_when_empty(self):
        html = _render(
            "{% card data_fragment=True data_state=state data_paused=paused data_ask_status=url %}A{% endcard %}",
            state="pending",
            paused=False,
            url="",
        )

        (card,) = with_class(html, "card")
        self.assertIn("data-fragment", card.attrs)
        self.assertEqual(card.attrs["data-state"], "pending")
        self.assertNotIn("data-paused", card.attrs)
        self.assertNotIn("data-ask-status", card.attrs)

    def test_what_a_page_gives_it_is_escaped(self):
        html = _render("{% card data_state=state label=state %}{{ state }}{% endcard %}", state='"><b>x</b>')

        (card,) = with_class(html, "card")
        self.assertEqual(elements(html, "b"), [])
        self.assertEqual(card.attrs["data-state"], '"><b>x</b>')

    def test_an_option_it_does_not_take_is_refused(self):
        for source in (
            '{% card class="x" %}A{% endcard %}',
            '{% card "x" %}A{% endcard %}',
            '{% card aria_hidden="true" %}A{% endcard %}',
        ):
            with self.subTest(source=source), self.assertRaises(TemplateSyntaxError):
                Template(source)


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


class ReadingTests(SimpleTestCase):
    PARTIALS = ("partials/_reading.html", "partials/_readings.html")

    def test_a_reading_leads_to_its_page_marked_as_its_part_of_a_region(self):
        html = _render(
            '{% readings %}{% reading icon="network" url="/example/" modal="example" tone="attention" '
            'note_tone="attention" data_fragment_part="example" %}Loopback{% lines %}<span>TLS 1.3</span>'
            "{% note %}A check failed{% endreading %}{% endreadings %}"
        )

        (row,) = with_class(html, "readings")
        (reading,) = with_class(html, "reading")
        (note,) = with_class(html, "reading-standing")
        self.assertIn(row, reading.ancestors)
        self.assertEqual(reading.tag, "a")
        self.assertEqual(reading.attrs["href"], "/example/")
        self.assertEqual(reading.attrs["data-modal-open"], "example")
        self.assertEqual(reading.attrs["data-fragment-part"], "example")
        self.assertIn("tone-attention", reading.classes)
        self.assertIn("is-attention", note.classes)
        self.assertEqual(texts(html, "strong"), ["Loopback"])

    def test_a_reading_with_a_panel_opens_it_in_place_by_its_id(self):
        html = _render(
            '{% reading icon="cloud" id="example-weather" data_refreshing=refreshing %}71°'
            '{% heading %}<strong>Weather</strong>{% panel %}<p>Mostly cloudy</p>{% foot %}<a href="/s/">Settings</a>'
            "{% endreading %}",
            refreshing=True,
        )

        (reading,) = with_class(html, "reading")
        (head,) = elements(html, "button")
        (panel,) = with_class(html, "reading-panel")
        self.assertIn("data-menu", reading.attrs)
        self.assertEqual(reading.attrs["data-refreshing"], "")
        self.assertEqual(head.attrs["popovertarget"], "example-weather")
        self.assertEqual(panel.attrs["id"], "example-weather")
        self.assertIn("popover", panel.attrs)
        self.assertEqual(with_class(html, "reading-panel-foot")[0].tag, "footer")

    def test_a_neutral_reading_takes_no_tone(self):
        html = _render('{% reading icon="cloud" url="/" tone="neutral" %}1{% endreading %}')

        (reading,) = with_class(html, "reading")
        self.assertFalse({word for word in reading.classes if word.startswith("tone-")})

    def test_a_reading_s_tone_is_one_the_status_vocabulary_names(self):
        for source in (
            '{% reading icon="cloud" url="/" tone="green" %}1{% endreading %}',
            '{% reading icon="cloud" url="/" note_tone="good" %}1{% endreading %}',
        ):
            with self.subTest(source=source), self.assertRaises(TemplateSyntaxError):
                _render(source)


@tag("source", "tree")
class SharedMarkupTemplateTests(SimpleTestCase):
    """No template writes by hand what a shared partial or tag draws."""

    def test_only_the_partial_prints_a_form_s_own_errors(self):
        found = _written_by_hand(r"\.non_field_errors", FormErrorsTests.PARTIAL)
        self.assertEqual(found, [], f'include "{FormErrorsTests.PARTIAL}" instead')

    def test_only_the_tag_draws_a_section_head(self):
        found = _written_by_hand(r'class="section-head[" ]', SectionHeadTests.PARTIAL)
        self.assertEqual(found, [], "write {% section %} instead")

    def test_only_the_tag_draws_a_card(self):
        found = _written_by_hand(r'\bclass="(?:[^"]*[\s}])?card[\s"{]', CardTests.PARTIAL)
        self.assertEqual(found, [], "write {% card %} instead")

    def test_only_the_tags_draw_a_reading(self):
        # A reading's classes are a shared part's, so the page-class check
        # lets a page write them; this is what keeps them the tags' own.
        reading = r'\bclass="(?:[^"]*[\s}])?reading(?:s|-head|-mark|-lead|-lines|-standing|-panel[a-z-]*)?[\s"{]'
        found = [name for name, text in _templates() if name not in ReadingTests.PARTIALS and re.search(reading, text)]
        self.assertEqual(found, [], "write {% readings %} and {% reading %} instead")

    def test_no_template_decides_a_fact_s_tone(self):
        # The tag refuses the part when a page renders; this reads every
        # template, rendered by a test or not.
        found = [
            f"{name}:{number}"
            for name, text in _templates()
            for number, line in enumerate(text.splitlines(), 1)
            if re.search(r"\{%\s*tone\s*%\}", line)
        ]
        self.assertFalse(found, "give the fact's projection a tone and write tone=<it> on {% fact %}")

    def test_only_the_outside_link_partial_writes_an_outside_link(self):
        # An anchor to a web address, or one that leaves in a new tab.
        outside = r'<a\b[^>]*(?:href="https?:|rel="noopener|target="_blank"|class="external-link)'
        found = _written_by_hand(outside, OutsideLinkTests.PARTIAL)
        self.assertEqual(found, [], "write {% entity url|outside label=name %} or {% outside_link %} instead")
