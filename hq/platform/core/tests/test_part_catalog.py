"""The browser survey reads a part through the catalog, so each form a part
takes is drawn there at least once, and each notice a script adds is the part
the server drew."""

import inspect
import re
from pathlib import Path

from django.conf import settings
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, override_settings, tag

from hq.platform.application.markup_testing import all_elements
from hq.platform.core.testing import render
lazy from hq.platform.core.tests.browser.catalog import CATALOG


def catalog_templates() -> dict[str, str]:
    """Each catalog entry's template, by its name."""

    return {name: entry.template for name, entry in CATALOG.items()}


@tag("source")
class CatalogDrawsEachFormTests(SimpleTestCase):
    def test_every_tone_of_the_notice_tag_has_a_catalog_entry(self):
        """A tone counts where an entry draws it, as the page's messages draw
        each of theirs."""

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

    def test_the_catalog_draws_a_select_in_a_tone_one_a_control_label_names_and_one_that_narrows(self):
        drawn = re.findall(r"\{% select (.*?)%\}", " ".join(catalog_templates().values()))
        self.assertTrue(any("id=" in found for found in drawn), "draw a select its control label names")
        self.assertTrue(any("tone=" in found for found in drawn), "draw a select in a tone")
        self.assertTrue(any("every=" in found for found in drawn), "draw a select that narrows")

    def test_every_option_and_part_of_the_filter_bar_has_a_catalog_entry(self):
        from hq.platform.core.templatetags.filter_tags import register

        takes = register.tags["filter_bar"].__wrapped__
        options = [
            option
            for option, parameter in inspect.signature(takes).parameters.items()
            if parameter.kind is inspect.Parameter.KEYWORD_ONLY
        ]
        entries = " ".join(t for name, t in catalog_templates().items() if name.startswith("form/filter-bar"))
        opened = " ".join(re.findall(r"\{% filter_bar (.*?)%\}", entries))
        self.assertEqual([o for o in options if f"{o}=" not in opened], [], "add a form/filter-bar-* entry")
        self.assertIn("data_", opened)
        self.assertIn("{% summary %}", entries)
        self.assertIn("{% choices %}", entries)


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
