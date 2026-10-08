"""The API reference page: the operator's, under HQ's policy, from a pinned bundle."""

import hashlib
import re
from functools import cache
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.templatetags.static import static
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

STATIC = Path(settings.BASE_DIR) / "static"
VENDOR = STATIC / "vendor" / "scalar"


@cache
def _source(*parts: str) -> str:
    return STATIC.joinpath(*parts).read_text(encoding="utf-8")


def _uncommented(source: str) -> str:
    return re.sub(r"/\*.*?\*/|^\s*//[^\n]*", " ", source, flags=re.DOTALL | re.MULTILINE)


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
        self.assertContains(response, static("css/api-reference.css"))
        # Something is said while the bundle loads, outside what it mounts on.
        self.assertContains(response, 'role="status">Loading the reference.</p>\n<div class="api-reference"')

    def test_it_sends_hqs_policy_minus_only_trusted_types(self):
        """The one exception to the policy, pinned.

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
        recorded = dict(line.split(": ", 1) for line in (VENDOR / "UPSTREAM").read_text(encoding="utf-8").splitlines())
        digest = hashlib.sha256((VENDOR / "standalone.js").read_bytes()).hexdigest()
        self.assertEqual(digest, recorded["sha256"])


class ReferenceStyleTests(SimpleTestCase):
    """The viewer is drawn from HQ's tokens, through names the bundle reads."""

    def setUp(self):
        self.sheet = _uncommented(_source("css", "api-reference.css"))

    def test_it_carries_no_colour_size_or_face_of_its_own(self):
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?|oklch|color-mix)\(", self.sheet), [])
        self.assertNotIn("font-family", self.sheet)
        self.assertEqual(re.findall(r"font-size:\s*[\d.]", self.sheet), [])

    def test_every_token_it_reads_is_one_app_css_defines(self):
        tokens = set(re.findall(r"^\s*(--[\w-]+):", _source("css", "app.css"), flags=re.MULTILINE))
        read = set(re.findall(r"var\((--[\w-]+)", self.sheet))
        self.assertGreater(len(read), 20)
        self.assertEqual(read - tokens, set())

    def test_every_variable_it_sets_is_one_the_bundle_reads(self):
        """A renamed variable would leave the viewer in the vendor's default."""

        bundle = _source("vendor", "scalar", "standalone.js")
        handed = set(re.findall(r"^\s*(--scalar-[\w-]+):", self.sheet, flags=re.MULTILINE))
        self.assertGreater(len(handed), 40)
        self.assertEqual({name for name in handed if name not in bundle}, set())

    def test_both_modes_take_the_same_tokens(self):
        """One block for the two mode classes: `color-scheme` picks the half."""

        self.assertEqual(
            re.findall(r"^([^{}\n]*-mode[^{}\n]*)\{", self.sheet, flags=re.MULTILINE), [".light-mode, .dark-mode "]
        )


class ReferenceConfigurationTests(SimpleTestCase):
    """What the page asks of the viewer, read from the script that mounts it."""

    def setUp(self):
        self.script = _uncommented(_source("js", "api-reference.js"))
        self.bundle = _source("vendor", "scalar", "standalone.js")

    def test_nothing_reaches_or_links_to_the_vendor(self):
        for option in (
            "withDefaultFonts: false",
            "telemetry: false",
            "hideClientButton: true",
            'showDeveloperTools: "never"',
            "agent: { disabled: true }",
            "mcp: { disabled: true }",
        ):
            with self.subTest(option=option):
                self.assertIn(option, self.script)
        # No address but this origin's, and no proxy to send a request through.
        self.assertEqual(re.findall(r"https?:|proxyUrl|pluginUrls|cdn", self.script), [])

    def test_no_request_is_sent_and_no_token_is_asked_for_or_kept(self):
        self.assertIn("hideTestRequestButton: true", self.script)
        self.assertEqual(re.findall(r"persistAuth|authentication", self.script), [])

    def test_the_theme_and_the_search_are_hqs_to_decide(self):
        for option in ('theme: "none"', "hideDarkModeToggle: true", "forceDarkModeState: theme"):
            with self.subTest(option=option):
                self.assertIn(option, self.script)
        self.assertNotIn("customCss", self.script)
        # HQ's own search answers to K.
        self.assertEqual(re.findall(r'searchHotKey: "(\w)"', self.script), ["j"])

    def test_every_option_is_one_the_bundle_knows(self):
        options = set(re.findall(r"^    (?:\.\.\.\(theme \? \{ )?(\w+):", self.script, flags=re.MULTILINE))
        self.assertGreater(len(options), 12)
        self.assertEqual({name for name in options if f"{name}:" not in self.bundle}, set())

    def test_curl_is_the_only_sample_offered(self):
        """Every target the bundle ships is hidden but the shell's curl."""

        shipped = re.search(r"\{(?:\w+:\[(?:`\w[\w.]*`,?)+\],?)+\}", self.bundle[self.bundle.index("`libcurl`") - 40 :])
        self.assertIsNotNone(shipped)
        targets = {
            target: re.findall(r"`([\w.]+)`", clients)
            for target, clients in re.findall(r"(\w+):\[([^\]]*)\]", shipped.group(0))
        }
        self.assertEqual(targets["shell"][0], "curl")
        hidden = set(re.findall(r'"(\w+)"', self.script[self.script.index("HIDDEN_CLIENTS") :].split("};")[0]))
        self.assertEqual(hidden, (set(targets) - {"shell"}) | set(targets["shell"][1:]))
