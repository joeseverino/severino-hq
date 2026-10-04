"""The API reference page: the operator's, under HQ's policy, from a pinned bundle."""

from __future__ import annotations

import hashlib
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.templatetags.static import static
from django.test import TestCase
from django.urls import reverse

VENDOR = Path(settings.BASE_DIR) / "static" / "vendor" / "scalar"


def _directives(policy: str) -> set[str]:
    return {" ".join(part.split()) for part in policy.split(";") if part.strip()}


class ReferencePageTests(TestCase):
    def setUp(self):
        self.url = reverse("api_reference:reference")

    def _sign_in(self):
        self.client.force_login(get_user_model().objects.create_user("operator"))

    def test_the_operator_gets_the_page_pointing_at_the_document(self):
        self._sign_in()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'data-url="{reverse("hq_api:openapi")}"')
        # Through the storage: collected and DEBUG off, the names are hashed.
        self.assertContains(response, static("vendor/scalar/standalone.js"))
        self.assertContains(response, static("js/api-reference.js"))

    def test_it_sends_hqs_policy_minus_only_trusted_types(self):
        """The page's one exception, pinned like the admin's.

        Scalar writes strings into innerHTML, so the two Trusted Types
        directives go and nothing else: no inline script, no eval, no origin
        but this one.
        """

        self._sign_in()
        application = self.client.get(reverse("dashboard"))["Content-Security-Policy"]
        page = self.client.get(self.url)["Content-Security-Policy"]
        removed = {d.split()[0] for d in _directives(application) - _directives(page)}
        self.assertEqual(removed, {"require-trusted-types-for", "trusted-types"})
        self.assertEqual(_directives(page) - _directives(application), set())
        self.assertIn("script-src 'self'", page)
        self.assertNotIn("unsafe-eval", page)

    def test_a_stranger_is_sent_to_sign_in(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(settings.LOGIN_URL))

    def test_the_nav_links_it(self):
        self._sign_in()
        self.assertContains(self.client.get(reverse("dashboard")), f'href="{self.url}"')

    def test_the_vendored_bundle_is_the_recorded_one(self):
        recorded = dict(
            line.split(": ", 1)
            for line in (VENDOR / "UPSTREAM").read_text(encoding="utf-8").splitlines()
        )
        digest = hashlib.sha256((VENDOR / "standalone.js").read_bytes()).hexdigest()
        self.assertEqual(digest, recorded["sha256"])
