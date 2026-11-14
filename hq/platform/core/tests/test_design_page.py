"""The Design page: every shared part, as the image build drew it once, in a
document of its own the page frames."""

import io
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from hq.platform.application.tests.markup import elements, select, texts
from hq.platform.core import design_gallery
from hq.platform.core.models import AuditLog
from hq.platform.core.tests.browser import catalog

# The most the gallery the image ships may weigh, in bytes, read whole by
# every first visit to the page: it grows by an entry's markup, so a new entry
# that crosses it is weighed before the line moves.
BUDGET = 650_000


class DesignPageTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(folder.cleanup)
        cls.built = Path(folder.name) / "design-gallery.html"
        call_command("design_gallery", output=cls.built, stdout=io.StringIO())
        cls.gallery = cls.built.read_text(encoding="utf-8")

    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("reader", password="unused-password"))

    def framed(self, response):
        return [frame.attrs["src"] for frame in elements(response, "iframe")]

    def test_the_build_draws_every_entry_under_its_name_within_its_budget(self):
        # An entry may hold regions of its own; each entry is one named for it.
        labels = [element.attrs.get("aria-label") for element in select(self.gallery, "role", "region")]
        drawn = [label for label in labels if label in catalog.CATALOG]

        self.assertEqual(drawn, [name for name, entry in catalog.CATALOG.items() if not entry.frame])
        self.assertLessEqual(len(self.gallery.encode()), BUDGET)
        # Without a token no example form can act, whoever presses it.
        self.assertNotIn("csrfmiddlewaretoken", self.gallery)

    def test_a_signed_in_reader_is_framed_what_the_build_drew_and_nothing_is_written(self):
        recorded = AuditLog.objects.count()

        with override_settings(SEVERINO_DESIGN_GALLERY=self.built):
            page = self.client.get(reverse("design"))
            (src,) = self.framed(page)
            document = self.client.get(src)

        self.assertEqual(page.status_code, 200)
        self.assertEqual(texts(page, "h1")[0], "Design")
        self.assertIn(reverse("design"), [link.attrs["href"] for link in select(page, "href")])
        # The examples stay in their own document, never in the page's.
        self.assertEqual(select(page, "role", "region"), [])
        self.assertEqual(document.status_code, 200)
        self.assertEqual(document.content.decode(), self.gallery)
        self.assertEqual(AuditLog.objects.count(), recorded)

    def test_only_hq_frames_the_gallery_and_it_runs_nothing(self):
        with override_settings(SEVERINO_DESIGN_GALLERY=self.built):
            (src,) = self.framed(self.client.get(reverse("design")))
            document = self.client.get(src)

        policy = document["Content-Security-Policy"]
        self.assertEqual(document["X-Frame-Options"], "SAMEORIGIN")
        self.assertIn("frame-ancestors 'self'", policy)
        self.assertIn("script-src 'none'", policy)
        self.assertIn("form-action 'none'", policy)

    def test_another_gallery_s_address_answers_nothing(self):
        with override_settings(SEVERINO_DESIGN_GALLERY=self.built):
            response = self.client.get(reverse("design_gallery", args=["0" * 16]))

        self.assertEqual(response.status_code, 404)

    def test_a_build_with_no_gallery_says_so(self):
        absent = self.built.with_name("absent.html")
        with override_settings(SEVERINO_DESIGN_GALLERY=absent):
            response = self.client.get(reverse("design"))
            document = self.client.get(reverse("design_gallery", args=[design_gallery.named(b"")]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(select(response, "data-empty-state")), 1)
        self.assertEqual(self.framed(response), [])
        self.assertEqual(document.status_code, 404)

    def test_a_dev_server_draws_it_live_from_the_catalog(self):
        live = '<div role="region" aria-label="example/live"></div>'
        with (
            override_settings(DEBUG=True, SEVERINO_DESIGN_GALLERY=self.built),
            mock.patch.object(catalog, "gallery_body", return_value=live),
        ):
            (src,) = self.framed(self.client.get(reverse("design")))
            document = self.client.get(src)

        self.assertEqual(
            [region.attrs["aria-label"] for region in select(document, "role", "region")], ["example/live"]
        )

    def test_signed_out_it_asks_for_a_sign_in(self):
        self.client.logout()

        for path in (reverse("design"), reverse("design_gallery", args=["0" * 16])):
            with self.subTest(path=path):
                response = self.client.get(path)

                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("login"), response["Location"])
