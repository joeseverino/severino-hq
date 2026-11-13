"""The section part: one look whether a template writes it or code builds it."""

import ast
import inspect
import re
from datetime import date
from pathlib import Path

from django.template import Context, Template, TemplateSyntaxError
from django.test import SimpleTestCase, tag
from django.utils.functional import SimpleLazyObject

from hq.platform.application.page_parts import SectionPart
from hq.platform.application.tests.markup import all_elements, elements, select, with_class
from hq.platform.core.templatetags.part_tags import SECTION

MONTH = date(2026, 11, 1)


def _render(source: str, **context) -> str:
    return Template(source).render(Context(context))


class SectionTagTests(SimpleTestCase):
    def test_a_section_is_named_by_its_heading_its_head_on_the_page_and_its_body_framed(self):
        html = _render(
            '{% section "Hosts" count=n lede="Each host HQ reads." %}<p>Body</p>'
            '{% aside %}<a href="/example/">All</a>{% endsection %}',
            n=3,
        )

        (landmark,) = elements(html, "section")
        (frame,) = with_class(html, "card")
        (heading,) = elements(html, "h2")
        (head,) = with_class(html, "section-head")
        (link,) = elements(html, "a")
        (body,) = [p for p in elements(html, "p") if "section-lede" not in p.classes]
        self.assertEqual(landmark.attrs["aria-labelledby"], heading.attrs["id"])
        self.assertNotIn("card", landmark.classes)
        self.assertIn(landmark, head.ancestors)
        self.assertIn(landmark, frame.ancestors)
        self.assertNotIn(frame, head.ancestors)
        self.assertIn(frame, body.ancestors)
        self.assertEqual(heading.words, "Hosts 3")
        self.assertEqual([count.words for count in with_class(html, "count")], ["3"])
        self.assertEqual([line.words for line in with_class(html, "section-lede")], ["Each host HQ reads."])
        self.assertIn(head, link.ancestors)
        self.assertNotIn(heading, link.ancestors)
        self.assertEqual([p.words for p in elements(html, "p") if "section-lede" not in p.classes], ["Body"])

    def test_a_count_of_zero_is_said(self):
        html = _render('{% section "Hosts" count=0 %}<p>Body</p>{% endsection %}')

        self.assertEqual([count.words for count in with_class(html, "count")], ["0"])

    def test_with_no_body_it_says_what_is_missing(self):
        html = _render(
            '{% section "Agents" %}{% if agents %}<p>Rows</p>{% endif %}'
            "{% empty %}No agent has connected yet.{% endsection %}",
            agents=[],
        )

        (frame,) = with_class(html, "section-body")
        (line,) = select(html, "data-empty-state")
        self.assertIn(frame, line.ancestors)
        self.assertEqual(line.words, "No agent has connected yet.")
        self.assertEqual(elements(html, "h2")[0].words, "Agents")

    def test_its_empty_line_offers_the_link_that_fills_it(self):
        html = _render(
            '{% section "Receipts" remedy_label="Upload one" remedy_url=url %}{% empty %}No receipts.{% endsection %}',
            url="/receipts/new/?related_expense=7",
        )

        (line,) = select(html, "data-empty-state")
        (link,) = elements(html, "a")
        self.assertIn(line, link.ancestors)
        self.assertEqual(link.attrs["href"], "/receipts/new/?related_expense=7")
        self.assertEqual(link.words, "Upload one")
        self.assertTrue(line.words.startswith("No receipts. Upload one"))

    def test_a_body_offers_no_remedy(self):
        html = _render(
            '{% section "Receipts" remedy_label="Upload one" remedy_url="/receipts/new/" %}<p>Rows</p>'
            "{% empty %}No receipts.{% endsection %}"
        )

        self.assertEqual(elements(html, "a"), [])

    def test_a_heading_with_no_words_is_the_moment_it_names(self):
        html = _render('{% section moment=month when="month" id="month" %}<p>Grid</p>{% endsection %}', month=MONTH)

        (landmark,) = elements(html, "section")
        (heading,) = elements(html, "h2")
        (moment,) = elements(html, "time")
        self.assertIn(heading, moment.ancestors)
        self.assertEqual(moment.attrs["datetime"], MONTH.isoformat())
        self.assertEqual(heading.words, moment.words)
        self.assertTrue(heading.words)
        self.assertEqual(landmark.attrs["aria-labelledby"], heading.attrs["id"])

    def test_a_moment_follows_the_heading_s_words(self):
        html = _render('{% section "Today" moment=day when="day" %}<p>Rows</p>{% endsection %}', day=MONTH)

        (heading,) = elements(html, "h2")
        (moment,) = elements(html, "time")
        self.assertIn(heading, moment.ancestors)
        self.assertTrue(heading.words.startswith("Today "))
        self.assertTrue(heading.words.endswith(moment.words))

    def test_with_no_body_and_nothing_to_say_it_draws_nothing(self):
        self.assertEqual(_render('{% section "Agents" %} {# none #} {% endsection %}').strip(), "")

    def test_a_body_draws_no_empty_line(self):
        html = _render('{% section "Agents" %}<p>Rows</p>{% empty %}None yet.{% endsection %}')

        self.assertEqual(select(html, "data-empty-state"), [])

    def test_a_section_in_a_section_heads_itself_a_level_down(self):
        html = _render(
            '{% section "Outer" %}{% section "Inner" %}<p>A</p>{% endsection %}{% endsection %}'
            '{% section "After" %}<p>B</p>{% endsection %}'
        )

        self.assertEqual([h.words for h in elements(html, "h2")], ["Outer", "After"])
        self.assertEqual([h.words for h in elements(html, "h3")], ["Inner"])

    def test_two_sections_of_one_heading_name_their_own(self):
        html = _render('{% section "Hosts" %}<p>A</p>{% endsection %}{% section "Hosts" %}<p>B</p>{% endsection %}')

        ids = [heading.attrs["id"] for heading in elements(html, "h2")]
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual([frame.attrs["aria-labelledby"] for frame in elements(html, "section")], ids)

    def test_its_id_and_data_options_are_the_frame_s(self):
        html = _render('{% section "Hosts" id="hosts" data_fragment="hosts" %}<p>A</p>{% endsection %}')

        (frame,) = elements(html, "section")
        self.assertEqual(frame.attrs["id"], "hosts")
        self.assertEqual(frame.attrs["data-fragment"], "hosts")
        self.assertNotEqual(elements(html, "h2")[0].attrs["id"], "hosts")

    def test_a_lazy_flag_is_a_flag_on_every_part_that_takes_data_options(self):
        # A context processor hands its values over lazily (agents_paused):
        # off is no attribute, never ="False", on every part drawing data-.
        parts = {
            "card": "{% card data_example=flag %}A{% endcard %}",
            "notice": "{% notice data_example=flag %}A{% endnotice %}",
            "section": '{% section "Hosts" data_example=flag %}<p>A</p>{% endsection %}',
            "status": '{% status "Online" data_example=flag %}',
        }
        for name, source in parts.items():
            for value in (False, None):
                with self.subTest(part=name, value=value):
                    self.assertEqual(
                        select(_render(source, flag=SimpleLazyObject(lambda value=value: value)), "data-example"), []
                    )
            with self.subTest(part=name, value=True):
                (drawn,) = select(_render(source, flag=SimpleLazyObject(lambda: True)), "data-example")
                self.assertEqual(drawn.attrs["data-example"], "")
            with self.subTest(part=name, value=0):
                (drawn,) = select(_render(source, flag=0), "data-example")
                self.assertEqual(drawn.attrs["data-example"], "0")

    def test_a_folded_section_s_heading_is_its_toggle(self):
        html = _render(
            '{% section "Dismissed" count=2 fold=True open=True %}{% for x in items %}{{ x }}{% empty %}<p>None</p>'
            "{% endfor %}{% endsection %}",
            items=[],
        )

        (fold,) = elements(html, "details")
        (summary,) = elements(html, "summary")
        (heading,) = elements(html, "h2")
        (frame,) = with_class(html, "card")
        self.assertIn("open", fold.attrs)
        self.assertEqual(fold.attrs["aria-labelledby"], heading.attrs["id"])
        self.assertIn(summary, heading.ancestors)
        # One head, the open section's: the toggle is the head, outside the frame.
        self.assertIn("section-head", summary.classes)
        self.assertNotIn(frame, summary.ancestors)
        self.assertIn(frame, elements(html, "p")[0].ancestors)
        self.assertEqual(heading.words, "Dismissed 2")
        # The for loop's own empty branch is the for loop's.
        self.assertEqual([p.words for p in elements(html, "p")], ["None"])

    def test_a_section_told_where_its_head_stands_says_so_and_keeps_its_head_and_body(self):
        source = '{% section "Hosts" HEAD%}<p>Body</p>{% aside %}<a href="/example/">View hosts</a>{% endsection %}'
        on_the_page = _render(source.replace("HEAD", ""))
        (plain,) = elements(on_the_page, "section")
        # Left alone, the stylesheet decides from the body.
        self.assertNotIn("data-head", plain.attrs)
        for head in ("above", "inside"):
            with self.subTest(head=head):
                told = _render(source.replace("HEAD", f'head="{head}" '))
                (landmark,) = elements(told, "section")
                self.assertEqual(landmark.attrs["data-head"], head)
                self.assertEqual(
                    [(e.tag, e.words) for e in all_elements(told)[1:]],
                    [(e.tag, e.words) for e in all_elements(on_the_page)[1:]],
                )

    def test_a_head_that_stands_neither_above_nor_inside_is_refused(self):
        with self.assertRaises(ValueError):
            _render('{% section "Hosts" head="beside" %}<p>Body</p>{% endsection %}')

    def test_its_foot_says_how_to_read_the_body_in_the_frame_after_it(self):
        html = _render(
            '{% section "Trends" %}<p>Body</p>{% aside %}<span>Weeks</span>'
            "{% foot %}It does not show cause.{% endsection %}"
        )

        (frame,) = with_class(html, "section-body")
        (foot,) = with_class(html, "section-foot")
        (body,) = [p for p in elements(html, "p") if "section-foot" not in p.classes]
        self.assertIn(frame, foot.ancestors)
        self.assertEqual(foot.words, "It does not show cause.")
        drawn = all_elements(html)
        self.assertLess(drawn.index(body), drawn.index(foot))
        (aside,) = with_class(html, "section-aside")
        self.assertEqual(aside.words, "Weeks")

    def test_with_no_body_its_foot_has_nothing_to_read(self):
        html = _render(
            '{% section "Trends" %}{% empty %}No trends yet.{% foot %}It does not show cause.{% endsection %}'
        )

        self.assertEqual(with_class(html, "section-foot"), [])
        self.assertEqual(select(html, "data-empty-state")[0].words, "No trends yet.")

    def test_its_aside_ends_with_the_link_to_the_rest(self):
        alone = _render(
            '{% section "Renewals" more_label="View renewals" more_url=url %}<p>Body</p>{% endsection %}',
            url="/example/",
        )
        (link,) = elements(alone, "a")
        (aside,) = with_class(alone, "section-aside")
        (head,) = with_class(alone, "section-head")
        self.assertIn(aside, link.ancestors)
        self.assertIn(head, aside.ancestors)
        self.assertEqual(link.attrs["href"], "/example/")
        self.assertTrue(link.words.startswith("View renewals"))

        after = _render(
            '{% section "Renewals" more_label="View renewals" more_url="/example/" %}<p>Body</p>'
            '{% aside %}<span class="count">3 due</span>{% endsection %}'
        )
        (aside,) = with_class(after, "section-aside")
        self.assertTrue(aside.words.startswith("3 due"))
        self.assertTrue(aside.words.removesuffix("→").strip().endswith("View renewals"))

    def test_its_link_to_the_rest_is_drawn_where_there_is_somewhere_to_go(self):
        html = _render('{% section "Plan" more_label="Change plan" more_url=url %}<p>Body</p>{% endsection %}', url="")

        self.assertEqual(elements(html, "a"), [])
        self.assertEqual(with_class(html, "section-aside"), [])

    def test_its_link_to_the_rest_says_where_it_goes(self):
        with self.assertRaises(ValueError):
            _render('{% section "Renewals" more_url="/example/" %}<p>Body</p>{% endsection %}')

    def test_a_figure_is_named_by_its_caption_which_is_its_head(self):
        html = _render(
            '{% section "Pace" figure=True lede="Each week." %}<svg role="img" aria-label="Pace"></svg>'
            "{% aside %}<span>Legend</span>{% endsection %}"
        )

        self.assertEqual(elements(html, "section"), [])
        (landmark,) = elements(html, "figure")
        (caption,) = elements(html, "figcaption")
        (heading,) = elements(html, "h2")
        (frame,) = with_class(html, "section-body")
        self.assertEqual(landmark.attrs["aria-labelledby"], heading.attrs["id"])
        self.assertIn("section-head", caption.classes)
        self.assertIn(caption, heading.ancestors)
        self.assertIn(caption, with_class(html, "section-aside")[0].ancestors)
        # The caption is the figure's first child, as HTML places one.
        self.assertEqual(next(e for e in all_elements(html) if landmark in e.ancestors), caption)
        self.assertNotIn(frame, caption.ancestors)

    def test_a_folded_section_s_toggle_holds_no_link(self):
        with self.assertRaises(ValueError):
            _render(
                '{% section "Runs" fold=True more_label="All runs" more_url="/example/" %}<p>Body</p>{% endsection %}'
            )

    def test_a_figure_does_not_fold(self):
        with self.assertRaises(ValueError):
            _render('{% section "Pace" figure=True fold=True %}<p>Body</p>{% endsection %}')

    def test_an_unknown_option_is_refused_as_the_template_compiles(self):
        with self.assertRaisesMessage(TemplateSyntaxError, "not tone"):
            Template('{% section "Hosts" tone="good" %}{% endsection %}')

    def test_a_section_needs_its_heading(self):
        with self.assertRaises(TemplateSyntaxError):
            Template("{% section count=1 %}{% endsection %}")


class SectionPartTests(SimpleTestCase):
    def test_code_and_template_draw_one_section(self):
        built = SectionPart(
            heading="Hosts", count=2, lede="Each host.", template="partials/_empty_state.html", context={"message": "A"}
        ).__html__()
        written = _render(
            '{% section "Hosts" count=2 lede="Each host." %}'
            '{% include "partials/_empty_state.html" with message="A" %}{% endsection %}'
        )

        self.assertEqual(built.split(), written.split())

    def test_code_and_template_draw_one_foot_link_and_figure(self):
        built = SectionPart(
            heading="Pace",
            foot="It does not show cause.",
            more_label="View runs",
            more_url="/example/",
            figure=True,
            template="partials/_empty_state.html",
            context={"message": "A"},
        ).__html__()
        written = _render(
            '{% section "Pace" more_label="View runs" more_url="/example/" figure=True %}'
            '{% include "partials/_empty_state.html" with message="A" %}{% foot %}It does not show cause.{% endsection %}'
        )

        self.assertEqual(built.split(), written.split())

    def test_a_line_given_in_code_is_text(self):
        html = SectionPart(heading="Pace", aside="3 < 4 & so", foot="n < 4 & so", template="partials/_empty_state.html")

        drawn = html.__html__()
        self.assertEqual(with_class(drawn, "section-aside")[0].words, "3 < 4 & so")
        self.assertEqual(with_class(drawn, "section-foot")[0].words, "n < 4 & so")
        self.assertEqual(drawn.count("&amp; so"), 2)

    def test_with_nothing_to_draw_it_draws_nothing(self):
        self.assertEqual(SectionPart(heading="Hosts").__html__(), "")
        self.assertEqual(len(select(SectionPart(heading="Hosts", empty="None yet.").__html__(), "data-empty-state")), 1)

    def test_its_heading_is_words(self):
        for heading in ("", "   ", "<b></b>"):
            with self.subTest(heading=heading), self.assertRaises(ValueError):
                SectionPart(heading=heading)

    def test_code_and_template_name_one_moment(self):
        built = SectionPart(moment=MONTH, when="month", template="partials/_empty_state.html", context={"message": "A"})
        written = _render(
            '{% section moment=month when="month" %}{% include "partials/_empty_state.html" with message="A" %}'
            "{% endsection %}",
            month=MONTH,
        )

        self.assertEqual(built.__html__().split(), written.split())

    def test_its_remedy_is_a_whole_link(self):
        for half in ({"remedy_label": "Upload one"}, {"remedy_url": "/receipts/new/"}):
            with self.subTest(**half), self.assertRaises(ValueError):
                SectionPart(heading="Receipts", empty="No receipts.", **half)

    def test_a_form_of_no_moment_is_refused(self):
        with self.assertRaises(ValueError):
            SectionPart(heading="Today", when="day")

    def test_its_level_is_one_html_has(self):
        for level in (1, 7):
            with self.subTest(level=level), self.assertRaises(ValueError):
                SectionPart(heading="Hosts", level=level)


CATALOG = Path(__file__).resolve().parent / "browser" / "catalog.py"


def _catalog_templates() -> dict[str, str]:
    """Each catalog entry's template, read from the module's source, which a
    source run reads without the browser group the module imports."""

    tree = ast.parse(CATALOG.read_text(encoding="utf-8"))
    (catalog,) = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and [getattr(t, "id", "") for t in node.targets] == ["CATALOG"]
    ]
    return {
        ast.literal_eval(key): entry.args[0].value
        for key, entry in zip(catalog.keys, catalog.values, strict=True)
        if isinstance(entry, ast.Call) and entry.args and isinstance(entry.args[0], ast.Constant)
    }


@tag("source")
class SectionCatalogTests(SimpleTestCase):
    def test_every_option_and_part_of_the_section_tag_has_a_catalog_entry(self):
        """The browser survey reads the part through the catalog, so each
        thing the tag takes is drawn there at least once."""

        taken = [
            *(
                f"{name}="
                for name, parameter in inspect.signature(SECTION).parameters.items()
                if parameter.kind is inspect.Parameter.KEYWORD_ONLY
            ),
            "data_",
            *(f"{{% {part} %}}" for part in SECTION.parts if part != "content"),
        ]
        sections = [template for template in _catalog_templates().values() if "{% section " in template]
        opened = " ".join(" ".join(re.findall(r"\{% section (.*?)%\}", template)) for template in sections)
        drawn = {
            word
            for word in taken
            if (
                re.search(rf"\b{re.escape(word)}", opened)
                if word.endswith(("=", "_"))
                else any(word in t for t in sections)
            )
        }
        self.assertEqual(
            sorted(set(taken) - drawn), [], "add a part/section-* entry to browser/catalog.py that draws it"
        )
