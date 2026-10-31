"""The host's templates as a contract: what an extension may use, and the record of it."""

import io
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from django.template import engines
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, override_settings, tag

from hq_sdk import contract, template_contract, text_contract, validation
from hq_sdk.money import money
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
        '{% for field in form %}{% include "partials/_dropzone.html" %}{% endfor %}\n'
        '{% include "example/_own.html" %}'
    ),
    "templates/example/_own.html": "{% load example_tags %}{{ value|doubled }}{% lorem 1 w %}",
    "templates/example/tags.html": (
        "{% load nav_tags %}\n{% problems_about %}\n{{ value|readable }}\n{{ count|counted }}\n"
        '{% post_button "Go" url color="red" %}'
    ),
    "templates/example/markup.html": (
        '<div class="kv invented {% if on %}pill-good{% endif %} pill-{{ tone }}">\n<p data-secret>x</p></div>'
    ),
    "templates/example/parts.html": (
        "{% card %}{% section_head %}A{% aside %}B{% endsection_head %}{% aside %}C{% endcard %}\n"
        "{% aside %}D\n"
        '{% card css="kv invented-card" %}{% endcard %}{% card css=chosen %}{% endcard %}\n'
        '{% post_button "Go" url css="invented-button" %}\n{% table_heading "A" note_css="invented-note" %}'
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
            "tags.html:5": "color",
            "markup.html:1": "invented",
            "markup.html:2": "data-secret",
            "parts.html:1": "aside",
            "parts.html:2": "aside",
            "parts.html:3": "invented-card",
            "parts.html:4": "invented-button",
            "parts.html:5": "invented-note",
        }
        for place, name in expected.items():
            with self.subTest(place=place):
                self.assertTrue(any(place in line and name in line for line in found), f"{place} {name} not in {found}")
        self.assertEqual(len(found), len(expected), found)


# The shapes a report can lose: a violation inside a block override, the same
# violation twice in one template and twice on one line, a template no view
# renders, a template that is not HTML, and an include named at render time.
EVERY = {
    "example_pkg/__init__.py": "",
    "example_pkg/templates/example/list.html": (
        '{% extends "list_page.html" %}\n{% block page %}\n{{ block.super }}\n'
        '{% include "partials/_modal.html" with id="import" %}\n{% endblock %}\n'
        "{% block row %}<td>{{ row.name }}</td>{% endblock %}"
    ),
    "example_pkg/templates/example/detail.html": (
        '{% extends "page.html" %}\n{% block page %}\n{% if empty %}\n'
        '{% include "partials/_empty_state.html" with message="None." compact=True %}\n{% else %}\n'
        '{% include "partials/_empty_state.html" with message="Nothing." compact=True %}\n{% endif %}\n'
        '{% include "partials/_empty_state.html" with message="A." compact=True %}'
        '{% include "partials/_empty_state.html" with message="B." compact=True %}\n{% endblock %}'
    ),
    "example_pkg/templates/example/_unrendered.html": '{% include "partials/_modal.html" %}',
    "example_pkg/templates/example/email.txt": "{{ total|readable }}",
    "example_pkg/templates/example/chosen.html": "{% include chosen %}",
    "example_pkg/templates/example/icon.png": b"\x89PNG\r\n\x1a\n\xff\xfe",
}


class TemplateValidationCompletenessTests(SimpleTestCase):
    def test_every_violation_in_every_template_is_reported(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name, content in EVERY.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                if isinstance(content, bytes):
                    (root / name).write_bytes(content)
                else:
                    (root / name).write_text(content, encoding="utf-8")
            found = template_violations(root)

        def at(place: str, name: str) -> list[str]:
            return [line for line in found if f"/{place}:" in line and name in line]

        self.assertEqual(len(at("list.html:2", "block page")), 1, found)
        self.assertEqual(len(at("list.html:4", "partials/_modal.html")), 1, found)
        self.assertEqual(len(at("detail.html:4", "compact")), 1, found)
        self.assertEqual(len(at("detail.html:6", "compact")), 1, found)
        self.assertEqual(len(at("detail.html:8", "compact")), 2, found)
        self.assertEqual(len(at("_unrendered.html:1", "partials/_modal.html")), 1, found)
        self.assertEqual(len(at("email.txt:1", "readable")), 1, found)
        self.assertEqual(len(at("chosen.html:1", "render time")), 1, found)
        self.assertEqual(len(found), 9, found)

    def test_a_root_with_no_package_fails_rather_than_passes(self):
        with (
            TemporaryDirectory() as directory,
            mock.patch("sys.argv", ["validation", directory]),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(validation.main(), 2)


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

        self.assertEqual(differences, [], "Run `mise run fix` and review the words that changed.")

    @tag("source")
    def test_plugins_md_states_the_recorded_contract(self):
        from hq.platform.core.management.commands.sdk_contract import documented, plugins_doc

        text = plugins_doc().read_text(encoding="utf-8")
        section = template_contract.markdown(contract.load_committed()["templates"])

        self.assertEqual(documented(text, section), text, "Run `mise run fix`.")


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
    def test_plain_reads_no_break_spaces_as_spaces_and_drops_word_joiners(self):
        self.assertEqual(plain("2\xa0weeks, 3 days"), "2 weeks, 3 days")
        self.assertEqual(plain(b"2&nbsp;weeks 2&#160;days 2\xc2\xa0hours"), "2 weeks 2 days 2 hours")
        self.assertEqual(plain(money(-5)), "\u2212$5.00")
        self.assertEqual(plain(b"\xe2\x88\x92&#8288;$5"), "\u2212$5")

    @override_settings(SEVERINO_SITE_HOST="hq.example.com")
    def test_the_site_host_is_the_deployments(self):
        self.assertEqual(host(), "hq.example.com")
