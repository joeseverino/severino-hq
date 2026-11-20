"""The notice: every banner and message a page shows is one part, in its tone."""

import re
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.messages.storage.base import Message
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, override_settings, tag

from hq.platform.application.markup_testing import all_elements, with_class
from hq.platform.core.testing import render
from hq.platform.core.tests.browser.catalog import CATALOG


class NoticeTagTests(SimpleTestCase):
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
    def test_every_tone_of_the_notice_tag_has_a_catalog_entry(self):
        """The browser survey reads the part through the catalog, so each
        tone it draws is drawn there at least once. A tone counts where an
        entry draws it, as the page's messages draw each of theirs."""

        from hq.platform.core.templatetags.notice_tags import TONES

        entries = [
            entry for name, entry in CATALOG.items() if name.startswith("part/notice-") or name == "part/messages"
        ]
        drawn = {
            name
            for entry in entries
            for each in all_elements(render(entry.template, **entry.context))
            for name in each.classes
        }
        drawn_tones = {tone for tone, classes in TONES.items() if tone and classes.split()[-1] in drawn}
        self.assertEqual(sorted(set(filter(None, TONES)) - drawn_tones), [], "add a part/notice-* entry in it")


# A tone a script clones a notice in: from template[data-notice="<tone>"],
# or through fragment.js's notice("<tone>").
_CLONED = re.compile(r"""(?:data-notice=["']|\bnotice\(")(\w+)""")
_FRAME = re.compile(r'<template data-notice="(\w+)">(.*?)</template>', re.DOTALL)


@tag("source", "tree")
class NoticeFrameTests(SimpleTestCase):
    def test_every_tone_a_script_clones_is_an_empty_frame_where_it_runs(self):
        """A script never writes a notice's class: it clones the part the
        server drew empty in base.html, so the line it adds is that part.
        HQ's scripts run on every page, demo.js only in the saved copy, and
        no tone is drawn that nothing clones."""

        root = Path(settings.BASE_DIR)
        scripts = {
            False: sorted((root / "static" / "js").rglob("*.js")),
            True: [*sorted((root / "static" / "js").rglob("*.js")), root / "demo" / "demo.js"],
        }
        request = RequestFactory().get("/")
        request.user = None
        for saved, paths in scripts.items():
            cloned = {tone for path in paths for tone in _CLONED.findall(path.read_text(encoding="utf-8"))}
            with override_settings(SEVERINO_RENDERED_COPY=saved):
                frames = dict(_FRAME.findall(render_to_string("base.html", {}, request=request)))

            self.assertTrue(cloned, "the scripts clone no notice: the pattern no longer reads them")
            self.assertEqual(sorted(set(frames) ^ cloned), [], "draw an empty {% notice %} of each tone cloned")
            for tone in cloned:
                with self.subTest(tone=tone, saved=saved):
                    (notice,) = all_elements(frames[tone])
                    self.assertIn(f"notice-{tone}", notice.classes)
                    self.assertEqual(notice.words, "")
