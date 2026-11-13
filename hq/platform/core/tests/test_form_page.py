"""Every page that is one form is drawn by one frame, ``form_page.html``."""

import re

from django import forms
from django.template import engines
from django.template.loader import render_to_string
from django.template.loader_tags import ExtendsNode
from django.test import SimpleTestCase, tag
from django.views.generic.edit import DeletionMixin, ModelFormMixin

from hq.platform.application.routes import routed
from hq.platform.application.tests.markup import elements, with_class
from hq.platform.application.tests.tree import ROOT
from hq.platform.core.tests.test_part_tags import _catalog_templates

FRAME = "form_page.html"
CARD = f"{FRAME}#form_card"
_COMMENT = re.compile(r"{%\s*comment\s*%}.*?{%\s*endcomment\s*%}|{#.*?#}", re.DOTALL)


def _frames(name: str) -> list[str]:
    """``name`` and every template it extends, nearest first."""

    engine = engines["django"].engine
    chain = [name]
    while True:
        nodes = engine.get_template(chain[-1]).nodelist.get_nodes_by_type(ExtendsNode)
        if not nodes:
            return chain
        chain.append(nodes[0].parent_name.resolve({}))


def form_pages() -> dict[str, str]:
    """Each routed page that creates or edits a record, and the template it draws."""

    return {
        route.path: route.view.template_name
        for route in routed()
        if isinstance(route.view, type)
        and issubclass(route.view, ModelFormMixin)
        and not issubclass(route.view, DeletionMixin)
    }


class ExampleForm(forms.Form):
    name = forms.CharField()


class FormPageTests(SimpleTestCase):
    def test_every_record_form_page_is_drawn_by_the_frame(self):
        pages = form_pages()

        self.assertGreater(len(pages), 1)
        self.assertEqual({route: name for route, name in pages.items() if FRAME not in _frames(name)}, {})

    def test_the_card_holds_the_form_its_fields_and_what_saving_does(self):
        html = render_to_string(
            CARD,
            {
                "form": ExampleForm(),
                "form_verb": "Add example",
                "cancel_url": "/example/",
                "form_note": "Saving files the example.",
            },
        )

        (card,) = with_class(html, "form-card")
        (drawn,) = elements(html, "form")
        self.assertIn(card, drawn.ancestors)
        self.assertEqual([e.attrs["name"] for e in elements(html, "input") if e.attrs["type"] == "text"], ["name"])
        (button,) = elements(html, "button")
        self.assertEqual(button.words, "Add example")
        self.assertIn("Saving files the example.", [e.words for e in with_class(html, "form-actions-note")])

    def test_a_form_of_no_fields_is_its_closing_row(self):
        html = render_to_string(CARD, {"form": forms.Form(), "form_verb": "Run example", "cancel_url": "/example/"})

        (drawn,) = elements(html, "form")
        self.assertEqual(with_class(html, "field"), [])
        (row,) = with_class(html, "form-actions")
        self.assertIn(drawn, row.ancestors)


@tag("source")
class FormCatalogTests(SimpleTestCase):
    def test_the_frames_card_and_a_form_that_asks_each_have_a_catalog_entry(self):
        """The browser survey reads the parts through the catalog: the card
        with fields and with none, and a query form."""

        entries = _catalog_templates()
        cards = [name for name, template in entries.items() if CARD in template]
        self.assertGreaterEqual(len(cards), 2, "add a form/* entry with fields and one with none")
        self.assertTrue([name for name, template in entries.items() if 'method="get"' in template])


@tag("source", "tree")
class FormCardTests(SimpleTestCase):
    def test_only_the_frame_draws_a_form_card(self):
        """A page that is one form extends the frame, so its card is never
        drawn by hand."""

        templates = ROOT / "templates"
        drawn = sorted(
            path.relative_to(templates).as_posix()
            for path in templates.rglob("*.html")
            if "form-card" in _COMMENT.sub("", path.read_text(encoding="utf-8"))
        )
        self.assertEqual(drawn, [FRAME], f'a page that is one form is {{% extends "{FRAME}" %}}')
