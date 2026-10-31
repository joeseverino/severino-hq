"""Every script carries the response's nonce, and waits for the page, except the two that must answer before it is drawn."""

import re
from pathlib import Path

from django.test import SimpleTestCase, tag

from .source import source_files, template_files

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = re.compile(r"<script\b[^>]*\bsrc=[^>]*>")
ANY_SCRIPT = re.compile(r"<script\b[^>]*>", re.IGNORECASE)
# The policy's script-src is the nonce and 'strict-dynamic' (settings.SECURE_CSP),
# so a tag without the response's nonce is refused in the browser.
NONCE = "{% csp_nonce_attr %}"
# `pagereveal` fires before the new page's first frame, which is before a
# deferred script runs, so its listener loads in the head without `defer`.
TRANSITION = "js/transitions.js"
# A table too wide for its region lets columns go in the frame it is laid out,
# which only a script watching each region from the moment it is parsed sees:
# CSS has no query for "this content overflows".
TABLES = "js/tables.js"
BEFORE_DRAWING = (TRANSITION, TABLES)
# A script only one page uses loads on that page, keyed by the attribute that
# marks the element it binds, so a page drawing that element without it is
# found here rather than as a control that does nothing.
PAGE_SCRIPTS = {"js/topology.js": "data-topology"}


@tag("source", "tree")
class ScriptLoadingTests(SimpleTestCase):
    def test_every_script_is_deferred_but_the_two_answering_before_the_first_frame(self):
        blocking = [
            f"{name}: {tag_}"
            for name, path in template_files()
            for tag_ in SCRIPT.findall(path.read_text(encoding="utf-8"))
            if not re.search(r"\bdefer\b|type=\"module\"", tag_) and not any(name in tag_ for name in BEFORE_DRAWING)
        ]
        self.assertEqual(blocking, [])

    def test_every_script_tag_carries_the_nonce(self):
        """Every template in the checkout, under templates/ and any app's own, and
        each installed extension's."""

        unnonced = [
            f"{path}: {tag_}"
            for path in sorted({*source_files(ROOT, "*.html"), *(path for _, path in template_files())})
            for tag_ in ANY_SCRIPT.findall(path.read_text(encoding="utf-8"))
            if NONCE not in tag_
        ]
        self.assertEqual(unnonced, [])

    def test_the_two_load_in_the_head_without_waiting(self):
        base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
        head = base[: base.index("</head>")]
        for name in BEFORE_DRAWING:
            with self.subTest(script=name):
                (found,) = [tag_ for tag_ in SCRIPT.findall(head) if name in tag_]
                self.assertNotRegex(found, r"\b(defer|async)\b")

    def test_the_page_transition_is_answered_only_where_it_loads_in_time(self):
        listeners = [
            path.name
            for path in source_files(ROOT / "static" / "js", "*.js")
            if re.search(r"[\"']page(reveal|swap)[\"']", path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(listeners, [Path(TRANSITION).name])

    def test_a_table_is_fitted_only_where_it_loads_in_time(self):
        fitters = [
            path.name
            for path in source_files(ROOT / "static" / "js", "*.js")
            if "data-dropped" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(fitters, [Path(TABLES).name])

    def test_a_page_script_loads_wherever_its_element_is_drawn(self):
        base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
        for script, hook in PAGE_SCRIPTS.items():
            with self.subTest(script=script):
                self.assertNotIn(script, base)
                marked = re.compile(rf"\s{re.escape(hook)}(?![\w-])")
                drawing = [
                    (name, path) for name, path in template_files() if marked.search(path.read_text(encoding="utf-8"))
                ]
                self.assertTrue(drawing, hook)
                self.assertEqual(
                    [name for name, path in drawing if script not in path.read_text(encoding="utf-8")],
                    [],
                )


@tag("source", "tree")
class TemplateSourceTests(SimpleTestCase):
    def test_application_templates_do_not_bypass_csp(self):
        violations = []
        for name, path in template_files():
            source = path.read_text(encoding="utf-8")
            if re.search(r"<script(?![^>]*\bsrc=)", source, re.IGNORECASE):
                violations.append(f"{name}: inline script")
            if re.search(r"\son[a-z]+\s*=", source, re.IGNORECASE):
                violations.append(f"{name}: inline event handler")

        self.assertEqual(violations, [])
