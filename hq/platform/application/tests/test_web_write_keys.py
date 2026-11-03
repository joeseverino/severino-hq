"""Every web write carries a retry key, so a form sent twice acts once.

Held twice: from the source, every form that posts, in the host's templates
and the example plugin's, draws its key with ``{% write_token %}``; and from
the registry, every capability that changes something refuses the browser
without one, so no write is left that a keyless form could reach.
"""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase, tag

from ..capabilities import capability_registry, run_capability
from ..security import Principal
from ..web_writes import RETRY_KEY_FIELD
from .source import source_files
from .writers import drawn_key, form_keyed

ROOT = Path(settings.BASE_DIR)
TEMPLATE_ROOTS = (ROOT / "templates", ROOT / "tests" / "fixtures" / "example_hq_plugin" / "templates")
# A sign-in changes nothing a capability runs, so it keeps the CSRF field alone.
SIGN_IN = ROOT / "templates" / "auth" / "login.html"
_FORM = re.compile(r"<form\b(?P<attributes>[^>]*)>(?P<body>.*?)</form>", re.DOTALL | re.IGNORECASE)
_POSTS = re.compile(r"""\bmethod\s*=\s*["']?post\b""", re.IGNORECASE)


def unkeyed_forms(source: str) -> list[str]:
    """The opening tag of each form in ``source`` that posts without ``{% write_token %}``."""

    return [
        f"<form{form['attributes']}>"
        for form in _FORM.finditer(source)
        if _POSTS.search(form["attributes"]) and "{% write_token %}" not in form["body"]
    ]


@tag("source", "tree")
class WriteFormTests(SimpleTestCase):
    def test_every_form_that_posts_draws_its_retry_key(self):
        templates = [path for root in TEMPLATE_ROOTS for path in source_files(root, "*.html")]
        self.assertTrue(templates, "the walk found no templates")

        found = [
            f"{path.relative_to(ROOT)}: {form}"
            for path in templates
            if path != SIGN_IN
            for form in unkeyed_forms(path.read_text(encoding="utf-8"))
        ]

        self.assertEqual(found, [], "Carry {% write_token %} in place of {% csrf_token %}.")

    def test_a_form_that_posts_without_the_tag_is_found_and_one_with_it_or_a_get_is_not(self):
        source = (
            '<form method="post">{% csrf_token %}</form>'
            "<form method=POST action=x>{% write_token %}</form>"
            '<form method="get"></form>'
        )

        self.assertEqual(unkeyed_forms(source), ['<form method="post">'])


class KeylessWebWriteTests(TestCase):
    def test_every_change_refuses_the_browser_without_a_key(self):
        specs = [spec for spec in capability_registry().values() if spec.effect != "read"]
        self.assertTrue(specs, "the registry holds no change")
        everything = frozenset(name for spec in specs for name in spec.required_capabilities)
        person = Principal("example-operator", "web", everything)

        refused = 0
        for spec in specs:
            with self.subTest(capability=spec.name):
                target = (1 if spec.target_kind == "integer" else "example") if spec.target_kind else None
                answer = run_capability(spec.name, {}, principal=person, target=target).answer
                code = answer.get("error", {}).get("code")
                # A grant held only by HQ itself (a logout token's) never reaches the browser at all.
                if code == "forbidden" and "never by a web principal" in answer["error"]["message"]:
                    continue

                self.assertEqual(code, "idempotency_key_required", answer)
                refused += 1
        self.assertTrue(refused, "no change was asked of the browser")


class RedrawnFormTests(TestCase):
    def test_a_form_drawn_again_after_its_write_draws_a_fresh_key(self):
        from django.contrib.auth import get_user_model

        self.client.force_login(get_user_model().objects.create_superuser("operator", "operator@example.com", "x" * 20))
        posted = form_keyed({"name": "Example", "slug": "example-redrawn"})

        first = self.client.post("/commands/project.create/", posted, follow=True)
        again = self.client.get("/commands/project.create/")

        self.assertEqual(first.status_code, 200)
        self.assertNotEqual(drawn_key(again, "data-command-form"), posted[RETRY_KEY_FIELD])
