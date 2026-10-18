"""The host's templates as a contract: what an extension may use, and the record of it."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from django.template import engines
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, override_settings, tag

from hq_sdk import contract, template_contract, text_contract
from hq_sdk.pages import PageAction, PageDialog, page_context
from hq_sdk.site import host
from hq_sdk.testing import plain
from hq_sdk.validation import template_violations

ROOT = Path(__file__).resolve().parents[4]

# One extension that steps outside the contract every way there is, beside
# what it may use of its own.
OUTSIDE = {
    "templates/example/shell.html": '{% extends "base.html" %}{% block content %}{% endblock %}',
    "templates/example/list.html": (
        '{% extends "list_page.html" %}\n{% block page %}{{ block.super }}{% endblock %}\n'
        "{% block row %}<td>{{ row.name }}</td>{% endblock %}"
    ),
    "templates/example/includes.html": (
        '{% include "partials/_modal.html" with id="x" %}\n'
        '{% include "partials/_empty_state.html" with message="None yet." compact=True %}\n'
        '{% for field in form %}{% include "partials/_form_field.html" %}{% endfor %}\n'
        '{% include "example/_own.html" %}'
    ),
    "templates/example/_own.html": '{% load example_tags %}{{ value|doubled }}{% now "Y" %}',
    "templates/example/tags.html": (
        "{% load nav_tags %}\n{% problems_about %}\n{{ value|readable }}\n{{ count|counted }}\n"
        '{% post_button "Go" url colour="red" %}'
    ),
    "templates/example/markup.html": (
        '<div class="card invented {% if on %}pill-good{% endif %} pill-{{ tone }}">\n<p data-secret>x</p></div>'
    ),
    "templatetags/example_tags.py": (
        "from django import template\n\nregister = template.Library()\n\n\n"
        "def doubled(value):\n    return value * 2\n\n\nregister.filter(doubled)\n"
    ),
}


class TemplateValidationTests(SimpleTestCase):
    def test_the_example_extension_uses_only_public_pieces(self):
        self.assertEqual(template_violations(ROOT / "tests" / "fixtures"), [])

    def test_every_way_outside_the_contract_is_named_where_it_is(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in OUTSIDE.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(text, encoding="utf-8")
            found = template_violations(root)

        expected = {
            "shell.html:1": "base.html",
            "list.html:2": "block page",
            "includes.html:1": "partials/_modal.html",
            "includes.html:2": "compact",
            "includes.html:3": "field",
            "tags.html:1": "nav_tags",
            "tags.html:2": "problems_about",
            "tags.html:3": "readable",
            "tags.html:4": "counted",
            "tags.html:5": "colour",
            "markup.html:1": "invented",
            "markup.html:2": "data-secret",
        }
        for place, name in expected.items():
            with self.subTest(place=place):
                self.assertTrue(any(place in line and name in line for line in found), f"{place} {name} not in {found}")
        self.assertEqual(len(found), len(expected), found)


class TemplateRecordTests(SimpleTestCase):
    def test_a_partials_parameters_are_what_it_reads(self):
        nodelist = (
            engines["django"]
            .from_string(
                "{% if why %}{{ why }}{% endif %}{{ message }}{% for item in items %}{{ item }}{% endfor %}"
                '{{ label|default:"" }}{% with total=items|length %}{{ total }}{% endwith %}'
            )
            .template.nodelist
        )
        reads = template_contract._Reads(engines["django"].engine)
        reads.walk(nodelist, set(), frozenset())

        self.assertEqual(reads.found, {"why": False, "message": True, "items": True, "label": False})

    def test_a_public_block_the_frame_stops_drawing_fails_the_record(self):
        with (
            mock.patch.dict(template_contract.FRAMES, {"list_page.html": ("row", "empty")}),
            self.assertRaisesMessage(ValueError, "empty"),
        ):
            template_contract.describe()

    def test_class_attributes_an_extension_declares_are_recorded(self):
        members = contract.describe()["modules"]["tables"]["TableListMixin"]["members"]

        self.assertEqual(members["table_empty"], {"kind": "attribute", "type": "str"})
        self.assertEqual(members["table_columns"], {"kind": "attribute", "type": "tuple"})

    def test_the_rendered_text_examples_are_current(self):
        differences = text_contract.drift(text_contract.load_committed(), text_contract.describe())

        self.assertEqual(differences, [], "Run `mise run regen` and review the words that changed.")

    @tag("source")
    def test_plugins_md_states_the_recorded_contract(self):
        from hq.platform.core.management.commands.sdk_contract import documented, plugins_doc

        text = plugins_doc().read_text(encoding="utf-8")
        section = template_contract.markdown(contract.load_committed()["templates"])

        self.assertEqual(documented(text, section), text, "Run `mise run regen`.")


class PageDialogTests(SimpleTestCase):
    def render(self, *actions):
        request = RequestFactory().get("/")
        return render_to_string("page.html", page_context("Example", actions=actions), request=request)

    def test_the_frame_draws_each_declared_dialog_once(self):
        dialog = PageDialog("import", "Import records", "partials/_empty_state.html", lede="Known files are skipped.")
        html = self.render(PageAction("Import", "/example/import/", primary=True, modal=dialog), PageAction("All", "/"))

        self.assertEqual(html.count('id="modal-import"'), 1)
        self.assertIn('href="/example/import/" data-modal-open="import"', html)

    def test_a_dialog_the_host_draws_elsewhere_is_only_opened(self):
        html = self.render(PageAction("How I am connected", "/connection/", modal="connection"))

        self.assertIn('data-modal-open="connection"', html)
        self.assertNotIn('id="modal-connection"', html)

    def test_a_dialog_needs_a_usable_id_and_a_get(self):
        with self.assertRaises(ValueError):
            PageDialog("Import Records", "Import", "example/_form.html")
        with self.assertRaises(ValueError):
            PageAction("Go", "/", method="post", modal=PageDialog("go", "Go", "example/_form.html"))


class TextHelperTests(SimpleTestCase):
    def test_plain_reads_no_break_spaces_as_spaces(self):
        self.assertEqual(plain("2\xa0weeks, 3 days"), "2 weeks, 3 days")
        self.assertEqual(plain(b"2&nbsp;weeks 2&#160;days 2\xc2\xa0hours"), "2 weeks 2 days 2 hours")

    @override_settings(SEVERINO_SITE_HOST="hq.example.com")
    def test_the_site_host_is_the_deployments(self):
        self.assertEqual(host(), "hq.example.com")
