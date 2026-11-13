"""The notice: every banner and message a page shows is one part, in its tone."""

import inspect
import re
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.messages.storage.base import Message
from django.template import Context, Template, TemplateSyntaxError
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, tag

from hq.platform.application.tests.markup import all_elements, elements, select, with_class
from hq.platform.application.tests.reads import reads
from hq.platform.core.tests.test_part_tags import _catalog_templates
from hq_sdk.testing import plain

# A tone's look is its class, which the stylesheet's tone assignment colors.
LOOKS = {"": None, "info": "notice-info", "success": "notice-success", "warning": "notice-warning"}


def _render(source: str, **context) -> str:
    return Template(source).render(Context(context))


class NoticeTagTests(SimpleTestCase):
    def test_each_tone_is_one_part_in_its_look_and_a_status(self):
        for tone, look in LOOKS.items():
            with self.subTest(tone=tone):
                html = _render("{% notice tone=tone %}Saved.{% endnotice %}", tone=tone)

                (notice,) = with_class(html, "notice")
                self.assertEqual(notice.attrs["role"], "status")
                self.assertEqual(notice.words, "Saved.")
                self.assertEqual([c for c in notice.classes if c.startswith("notice-")], [look] if look else [])

    def test_an_error_interrupts(self):
        html = _render('{% notice tone="error" %}It failed.{% endnotice %}')

        (notice,) = with_class(html, "notice")
        self.assertIn("notice-error", notice.classes)
        self.assertEqual(notice.attrs["role"], "alert")

    def test_its_title_leads_its_body(self):
        html = _render(
            '{% notice tone="warning" title=title %}<p>{{ names }}</p>{% endnotice %}',
            title="2 jobs failed",
            names="backup, sync",
        )

        (notice,) = with_class(html, "notice")
        (title,) = elements(html, "strong")
        (body,) = elements(html, "p")
        self.assertEqual(title.words, "2 jobs failed")
        self.assertIn(notice, title.ancestors)
        self.assertIn(notice, body.ancestors)
        self.assertLess(plain(html).index("2 jobs failed"), plain(html).index("backup, sync"))

    def test_a_title_the_page_composes_is_its_title_part(self):
        html = _render(
            '{% notice tone="warning" %}<p>Pick another.</p>{% title %}{{ name }} cannot be used{% endnotice %}',
            name="example-target",
        )

        (notice,) = with_class(html, "notice")
        (title,) = elements(html, "strong")
        self.assertEqual(title.words, "example-target cannot be used")
        self.assertIn(notice, title.ancestors)
        self.assertLess(plain(html).index("cannot be used"), plain(html).index("Pick another."))

    def test_a_pages_class_and_data_attributes_ride_on_it(self):
        html = _render(
            '{% notice css="example" data_example_ref=ref data_example_flag=True data_example_off=False %}'
            "Body{% endnotice %}",
            ref='a"b',
        )

        (notice,) = select(html, "data-example-ref", 'a"b')
        self.assertIn("example", notice.classes)
        self.assertIn("notice", notice.classes)
        self.assertEqual(notice.attrs["data-example-flag"], "")
        self.assertNotIn("data-example-off", notice.attrs)

    def test_with_no_body_it_is_the_frame_a_script_fills(self):
        html = _render('{% notice tone="error" %}{% endnotice %}')

        (notice,) = with_class(html, "notice")
        self.assertEqual(notice.words, "")
        self.assertEqual(notice.attrs["role"], "alert")

    def test_its_body_is_escaped_as_the_page_writes_it(self):
        html = _render("{% notice title=title %}{{ body }}{% endnotice %}", title="<b>t</b>", body="<i>b</i>")

        self.assertEqual(elements(html, "b"), [])
        self.assertEqual(elements(html, "i"), [])

    def test_an_unknown_tone_or_option_is_refused(self):
        with self.assertRaisesMessage(TemplateSyntaxError, "tone= is one of error, info, success, warning"):
            _render('{% notice tone="attention" %}Body{% endnotice %}')
        with self.assertRaises(TemplateSyntaxError):
            _render('{% notice heading="Body" %}Body{% endnotice %}')

    def test_a_message_is_a_notice_in_its_levels_tone(self):
        request = RequestFactory().get("/")
        request.user = None
        said = [
            Message(messages.SUCCESS, "Saved."),
            Message(messages.WARNING, "Saved, with a gap."),
            Message(messages.ERROR, "Not saved."),
        ]

        html = render_to_string("base.html", {"messages": said}, request=request)

        (listed,) = with_class(html, "messages")
        notices = [notice for notice in with_class(html, "notice") if listed in notice.ancestors]
        self.assertEqual([notice.words for notice in notices], ["Saved.", "Saved, with a gap.", "Not saved."])
        self.assertIn("notice-success", notices[0].classes)
        self.assertIn("notice-warning", notices[1].classes)
        self.assertEqual([notice.attrs["role"] for notice in notices], ["status", "status", "alert"])


@tag("source")
class NoticeCatalogTests(SimpleTestCase):
    def test_every_tone_and_option_of_the_notice_tag_has_a_catalog_entry(self):
        """The browser survey reads the part through the catalog, so each
        tone it draws and each option it takes is drawn there at least once."""

        from hq.platform.core.templatetags.notice_tags import TONES, register

        takes = register.tags["notice"].__wrapped__
        options = [
            f"{name}="
            for name, parameter in inspect.signature(takes).parameters.items()
            if parameter.kind is inspect.Parameter.KEYWORD_ONLY and name != "tone"
        ]
        notices = [t for name, t in _catalog_templates().items() if name.startswith("part/notice-")]
        opened = " ".join(" ".join(re.findall(r"\{% notice (.*?)%\}", t)) for t in notices)
        drawn_options = {word for word in [*options, "data_"] if re.search(rf"\b{re.escape(word)}", opened)}
        written = " ".join(notices)
        drawn_tones = {tone for tone in TONES if tone and (f'tone="{tone}"' in written or f'"{tone}"' in written)}

        self.assertEqual(sorted({*options, "data_"} - drawn_options), [], "add a part/notice-* entry that draws it")
        self.assertEqual(sorted(set(filter(None, TONES)) - drawn_tones), [], "add a part/notice-* entry in it")
        self.assertTrue(any("{% notice" in t and "tone=" not in t for t in notices), "draw a neutral notice")


# A tone a script clones a notice in: from template[data-notice="<tone>"],
# or through fragment.js's notice("<tone>").
_CLONED = re.compile(r"""(?:data-notice=["']|\bnotice\(")(\w+)""")
_FRAME = re.compile(r'<template data-notice="(\w+)">(.*?)</template>', re.DOTALL)


@tag("source", "tree")
@reads("static/js/**/*.js", "demo/demo.js", "**/templates/**")
class NoticeFrameTests(SimpleTestCase):
    def test_every_tone_a_script_clones_is_an_empty_frame_on_every_page(self):
        """A script never writes a notice's class: it clones the part the
        server drew empty in base.html, so the line it adds is that part."""

        root = Path(settings.BASE_DIR)
        scripts = [*sorted((root / "static" / "js").rglob("*.js")), root / "demo" / "demo.js"]
        cloned = {tone for path in scripts for tone in _CLONED.findall(path.read_text(encoding="utf-8"))}
        request = RequestFactory().get("/")
        request.user = None
        frames = dict(_FRAME.findall(render_to_string("base.html", {}, request=request)))

        self.assertTrue(cloned, "the scripts clone no notice: the pattern no longer reads them")
        self.assertEqual(sorted(cloned - set(frames)), [], "draw an empty {% notice %} of that tone in base.html")
        for tone in cloned:
            with self.subTest(tone=tone):
                (notice,) = all_elements(frames[tone])
                self.assertIn(f"notice-{tone}", notice.classes)
                self.assertEqual(notice.words, "")
