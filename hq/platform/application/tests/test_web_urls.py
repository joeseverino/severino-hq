"""An href built from data someone else wrote is a web address or nothing."""

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from ..entity_links import declared_link, entity_link, web_url

HOSTILE = (
    "javascript:fetch('/api/v2/')",
    "JavaScript:alert(1)",
    " javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox",
    "https://",
    "//evil.example.com/path",
    "file:///etc/passwd",
)


class WebUrlTests(SimpleTestCase):
    def test_web_addresses_pass(self):
        for url in ("https://github.com/example/app", "http://app.example.com:8080/x?y=1"):
            with self.subTest(url=url):
                self.assertEqual(web_url(url), url)

    def test_anything_else_is_nothing(self):
        for url in (*HOSTILE, "", None, 42):
            with self.subTest(url=url):
                self.assertEqual(web_url(url), "")

    def test_a_reading_console_link_is_kept_only_when_it_is_a_web_address(self):
        # A repository reading's console is the url its record carries.
        safe = entity_link("github.repository", "", record={"repository": "example/app", "url": "https://github.com/example/app"})
        hostile = entity_link("github.repository", "", record={"repository": "example/app", "url": HOSTILE[0]})

        self.assertEqual(safe.url, "https://github.com/example/app")
        self.assertEqual((hostile.url, hostile.external), ("", False))

    def test_a_declared_link_keeps_hq_pages_and_web_addresses_only(self):
        self.assertEqual(declared_link("page", "/infrastructure/").url, "/infrastructure/")
        self.assertTrue(declared_link("site", "https://app.example.com/").external)
        for url in HOSTILE:
            with self.subTest(url=url):
                self.assertEqual(declared_link("target", url).url, "")


class RenderedLinkTests(TestCase):
    def test_a_manifest_url_that_is_not_a_web_address_renders_as_text(self):
        from hq.domains.docs_index.models import DocumentationRecord

        record = DocumentationRecord.objects.create(
            doc_id="example-doc", title="Example", external_url=HOSTILE[0]
        )
        self.client.force_login(get_user_model().objects.create_user("example-operator", password="x" * 20))

        body = self.client.get(reverse("docs_index:detail", args=[record.doc_id])).content.decode()

        self.assertNotIn('href="javascript:', body)
        self.assertIn("javascript:fetch", body)  # still shown, as text
