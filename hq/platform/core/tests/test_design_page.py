"""The Design page: every shared part, as the image build drew it once or as
the catalog draws it on a server that names no file, in a document of its own
the page frames."""

import io
import tempfile
import warnings
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings, tag
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

    def test_a_build_with_no_gallery_says_so_and_never_draws_one(self):
        absent = self.built.with_name("absent.html")
        for debug in (False, True):
            with (
                self.subTest(debug=debug),
                override_settings(DEBUG=debug, SEVERINO_DESIGN_GALLERY=absent),
                mock.patch.object(catalog, "gallery_body", side_effect=AssertionError("drawn live")),
            ):
                response = self.client.get(reverse("design"))
                documents = [
                    self.client.get(reverse("design_gallery", args=[name]))
                    for name in (design_gallery.named(b""), "live")
                ]

                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(select(response, "data-empty-state")), 1)
                self.assertEqual(self.framed(response), [])
                self.assertEqual([document.status_code for document in documents], [404, 404])
                self.assertFalse(absent.exists())

    def test_a_server_that_names_no_file_draws_it_live_from_the_catalog(self):
        # `mise run dev` runs with DEBUG off and `dev:scratch` with it on;
        # neither names a file, so each draws the catalog as it stands.
        for debug in (False, True):
            with self.subTest(debug=debug), override_settings(DEBUG=debug, SEVERINO_DESIGN_GALLERY=None):
                # The page frames the gallery and draws none of it itself.
                with mock.patch.object(catalog, "gallery_body", side_effect=AssertionError("drawn by the page")):
                    (src,) = self.framed(self.client.get(reverse("design")))
                for edit in ("first", "second"):
                    body = f'<div role="region" aria-label="example/{edit}"></div>'
                    with mock.patch.object(catalog, "gallery_body", return_value=body):
                        document = self.client.get(src)

                    self.assertEqual(
                        [region.attrs["aria-label"] for region in select(document, "role", "region")],
                        [f"example/{edit}"],
                    )
                    self.assertNotIn("immutable", document["Cache-Control"])

    def test_writing_it_needs_somewhere_to_write(self):
        with override_settings(SEVERINO_DESIGN_GALLERY=None), self.assertRaises(CommandError):
            call_command("design_gallery", stdout=io.StringIO())

    def test_its_example_forms_draw_without_a_missing_token_warning(self):
        # DEBUG is when Django warns of a form drawn with no token.
        with override_settings(DEBUG=True), warnings.catch_warnings():
            warnings.simplefilter("error")
            design_gallery.drawn()

    def test_signed_out_it_asks_for_a_sign_in(self):
        self.client.logout()

        for path in (reverse("design"), reverse("design_gallery", args=["0" * 16])):
            with self.subTest(path=path):
                response = self.client.get(path)

                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("login"), response["Location"])


@tag("source")
class TheImageNamesItsGalleryTests(SimpleTestCase):
    """A server that names no file draws the gallery from test code on each
    request, so the image names the file its build wrote before it serves."""

    def test_the_image_names_the_file_before_its_build_writes_it(self):
        lines = (Path(settings.BASE_DIR) / "Dockerfile").read_text(encoding="utf-8").replace("\\\n", "").splitlines()
        named = [
            at for at, line in enumerate(lines) if line.startswith("ENV ") and " SEVERINO_DESIGN_GALLERY=/" in line
        ]
        written = [at for at, line in enumerate(lines) if "manage.py design_gallery" in line]

        self.assertEqual(len(named), 1)
        self.assertEqual(len(written), 1)
        self.assertLess(named[0], written[0])
