"""Every form a page is about closes with one row, ``partials/_form_actions.html``.

A record's form and a command's end alike: the verb its view supplies, Cancel,
and what saving does (docs/Design.md, "Every form a page is about is drawn one
way"). Every host template that writes a form is read, whatever its name: one
that writes its own submit button or ``form-actions`` row has drawn a second
kind.
"""

import re

from django.test import SimpleTestCase, tag

from hq.platform.application.tests.test_hand_drawn import PARTS
from hq.platform.application.tests.tree import ROOT

TEMPLATES = ROOT / "templates"
PARTIAL = "partials/_form_actions.html"
# The form part itself, which every form that writes is drawn through.
FORM = "partials/_form.html"
# A form that writes: a query form (method get) closes with its own Filter.
_POSTS = re.compile(r"<form\b[^>]*\bmethod=\"post\"", re.IGNORECASE)
_OWN_ROW = re.compile(r"type=\"submit\"|class=\"[^\"]*\bform-actions\b|<button\b(?![^>]*type=\"button\")")
_COMMENT = re.compile(r"{%\s*comment\s*%}.*?{%\s*endcomment\s*%}|{#.*?#}", re.DOTALL)
_FORM = re.compile(r"<form\b.*?</form>", re.DOTALL | re.IGNORECASE)
# The forms that are one verb or none (a menu's button, a form a script
# submits), which test_hand_drawn names as parts of their own.
ONE_VERB = frozenset(name for name, shape in PARTS if shape == "a form that writes")
# Today's forms that close with a row of their own, each to move onto
# partials/_form.html; this only falls.
OWN_ROW_TODAY = frozenset(
    {
        "control_plane/zone_mail.html",
    }
)


def form_templates() -> dict[str, str]:
    """Each host template that draws a form which writes, by name."""

    found = {}
    for path in sorted(TEMPLATES.rglob("*.html")):
        source = _COMMENT.sub("", path.read_text(encoding="utf-8"))
        if _POSTS.search(source):
            found[path.relative_to(TEMPLATES).as_posix()] = source
    return found


def misses(source: str) -> str:
    """What a form template's forms do instead of closing with the shared row, or ""."""

    forms = _FORM.findall(source)
    if any(_OWN_ROW.search(form) for form in forms):
        return "writes its own submit row"
    if not all(PARTIAL in form for form in forms):
        return f"does not include {PARTIAL}"
    return ""


class MissesTests(SimpleTestCase):
    def test_a_form_that_closes_with_the_row_passes(self):
        source = f'<form method="post">{{{{ form }}}}{{% include "{PARTIAL}" with verb=form_verb only %}}</form>'
        self.assertEqual(misses(source), "")

    def test_its_own_submit_button_is_found(self):
        source = f'<form method="post"><button class="primary">Save</button>{{% include "{PARTIAL}" %}}</form>'
        self.assertEqual(misses(source), "writes its own submit row")

    def test_its_own_actions_row_is_found(self):
        source = '<form method="post"><div class="form-actions"><a class="btn" href="/">Cancel</a></div></form>'
        self.assertEqual(misses(source), "writes its own submit row")

    def test_a_form_with_no_row_is_found(self):
        self.assertEqual(misses('<form method="post">{{ form }}</form>'), f"does not include {PARTIAL}")

    def test_a_row_outside_the_form_passes(self):
        source = f'<div class="form-actions"><a href="/">Back</a></div><form method="post">{{% include "{PARTIAL}" %}}</form>'
        self.assertEqual(misses(source), "")

    def test_a_button_that_only_works_the_page_passes(self):
        source = f'<form method="post"><button type="button">Add another</button>{{% include "{PARTIAL}" %}}</form>'
        self.assertEqual(misses(source), "")


@tag("source", "tree")
class FormTemplateTests(SimpleTestCase):
    def test_every_form_closes_with_the_shared_row(self):
        forms = form_templates()

        self.assertIn(FORM, forms)
        self.assertEqual(
            [
                f"{name}: {missed}"
                for name, source in forms.items()
                if name not in ONE_VERB | OWN_ROW_TODAY and (missed := misses(source))
            ],
            [],
            f"A form closes with {{% include {PARTIAL!r} with verb=... cancel_url=... only %}}, its verb and its"
            " way back from the view",
        )

    def test_each_form_left_to_move_still_closes_its_own_way(self):
        forms = form_templates()
        self.assertEqual(sorted(name for name in OWN_ROW_TODAY if not misses(forms.get(name, ""))), [])
