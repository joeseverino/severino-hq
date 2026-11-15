"""The Design page: every shared part drawn as the page's own content, as the
image build drew it once or as the catalog draws it on a server that names no
file, each part in a tree of its own."""

import io
import os
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings, tag
from django.urls import reverse

from hq.platform.application.tests.markup import all_elements, elements, select
from hq.platform.core import design_gallery
from hq.platform.core.models import AuditLog
from hq.platform.core.tests.browser import catalog

# The most the gallery the image ships may weigh as a browser receives it
# (core.compression), in bytes, sent whole with every visit to the page: it
# grows by an entry's markup, so a new entry that crosses it is weighed before
# the line moves. Its raw size is mostly each shadow root's links to the
# sheets, which compress to nearly nothing. The line stands a third above the
# 44 KB the catalog weighs, room for the image's longer asset names.
BUDGET = 60_000

# What points at an id: an example's in the page's own tree could reach the
# page's elements, its one form with a token among them.
POINTERS = ("id", "for", "form", "popovertarget", "commandfor", "aria-controls", "aria-labelledby", "aria-describedby")


def _light(html):
    """The elements of the page's own tree, out of every shadow root."""

    return [element for element in all_elements(html) if not element.shadowed]


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

    def test_the_build_draws_every_entry_under_its_name_within_its_budget(self):
        labels = [element.attrs.get("aria-label") for element in select(self.gallery, "role", "region")]
        drawn = [label for label in labels if label in catalog.CATALOG]

        self.assertEqual(drawn, [name for name, entry in catalog.CATALOG.items() if not entry.frame])
        from hq.platform.core.compression import as_sent

        raw = self.gallery.encode()
        sent = len(as_sent(raw))
        self.assertLessEqual(sent, BUDGET, f"the gallery is {sent} bytes sent, {len(raw)} raw")
        # Without a token no example form can act, whoever presses it.
        self.assertNotIn("csrfmiddlewaretoken", self.gallery)

    def test_each_part_is_drawn_in_a_tree_of_its_own(self):
        # A region holds the entry's name in the page's tree and its part in
        # a shadow root, so nothing of an example meets the page or another.
        regions = [element for element in _light(self.gallery) if element.attrs.get("role") == "region"]
        roots = self.gallery.count('<template shadowrootmode="open">')
        ours = {f"design-{name.partition('/')[0]}" for name in catalog.CATALOG}

        self.assertEqual(roots, len(regions))
        self.assertEqual(
            [
                (element.tag, name)
                for element in _light(self.gallery)
                for name in POINTERS
                if name in element.attrs and element.attrs.get(name) not in ours
            ],
            [],
        )

    def test_no_part_takes_the_page_s_focus(self):
        # A part's autofocus would scroll the page to it on load, under the
        # families' map, which stays at the top.
        def focused(html):
            return [element.tag for element in all_elements(html) if "autofocus" in element.attrs]

        self.assertEqual(focused(catalog.CATALOG["form/filter-bar"].part("form/filter-bar")), ["input"])
        self.assertEqual(focused(self.gallery), [])

    def test_a_signed_in_reader_is_drawn_what_the_build_drew_and_nothing_is_written(self):
        recorded = AuditLog.objects.count()

        with override_settings(SEVERINO_DESIGN_GALLERY=self.built):
            page = self.client.get(reverse("design"))

        self.assertEqual(page.status_code, 200)
        # Each part that heads its own page draws its heading in its own tree.
        self.assertEqual([element.words for element in _light(page) if element.tag == "h1"], ["Design"])
        self.assertEqual(elements(page, "iframe"), [])
        self.assertIn(self.gallery, page.content.decode())
        self.assertEqual(AuditLog.objects.count(), recorded)

    def test_a_build_with_no_gallery_says_so_and_never_draws_one(self):
        absent = self.built.with_name("absent.html")
        for debug in (False, True):
            with (
                self.subTest(debug=debug),
                override_settings(DEBUG=debug, SEVERINO_DESIGN_GALLERY=absent),
                mock.patch.object(catalog, "gallery_body", side_effect=AssertionError("drawn live")),
            ):
                response = self.client.get(reverse("design"))

                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(select(response, "data-empty-state")), 1)
                self.assertFalse(absent.exists())

    def test_a_server_that_names_no_file_draws_it_live_from_the_catalog(self):
        # `mise run dev` runs with DEBUG off and `dev:scratch` with it on;
        # neither names a file, so each draws the catalog as it stands.
        for debug in (False, True):
            with self.subTest(debug=debug), override_settings(DEBUG=debug, SEVERINO_DESIGN_GALLERY=None):
                for edit in ("first", "second"):
                    body = f'<div role="region" aria-label="example/{edit}"></div>'
                    with mock.patch.object(catalog, "gallery_body", return_value=body):
                        page = self.client.get(reverse("design"))

                    self.assertEqual(
                        [region.attrs["aria-label"] for region in select(page, "role", "region")],
                        [f"example/{edit}"],
                    )

    def test_writing_it_needs_somewhere_to_write(self):
        with override_settings(SEVERINO_DESIGN_GALLERY=None), self.assertRaises(CommandError):
            call_command("design_gallery", stdout=io.StringIO())

    def test_the_build_s_command_line_opens_no_database(self):
        # The image build runs it as root, so a database it opened would ship
        # root's in the volume HQ's account migrates.
        database = self.built.with_name("never.sqlite3")
        environment = {**os.environ, "SEVERINO_DATABASE_PATH": str(database)}
        command = [sys.executable, "manage.py", "design_gallery", "--output", str(self.built.with_name("cli.html"))]

        subprocess.run(command, cwd=settings.BASE_DIR, env=environment, check=True, capture_output=True, timeout=300)

        self.assertTrue(self.built.with_name("cli.html").exists())
        self.assertFalse(database.exists())

    def test_its_example_forms_draw_without_a_missing_token_warning(self):
        # DEBUG is when Django warns of a form drawn with no token.
        with override_settings(DEBUG=True), warnings.catch_warnings():
            warnings.simplefilter("error")
            design_gallery.drawn()

    def test_signed_out_it_asks_for_a_sign_in(self):
        self.client.logout()

        response = self.client.get(reverse("design"))

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
