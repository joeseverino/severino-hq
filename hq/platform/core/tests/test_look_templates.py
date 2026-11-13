"""Every template HQ holds is one dev:look can be asked for by name, so the
survey a change to it owes can always run: each reaches a page the estate
links to, a component catalog entry, or a page ``all`` or a bare route name
reaches."""

import os
from unittest import mock

from django.test import TestCase, tag

from hq.platform.core.tests import look
from hq.platform.core.tests.look_records import EstatePages
from hq.platform.core.tests.task import ROOT

ASKED = {"usage_pages": "", "usage_seed": "showcase"}
TEMPLATES = ROOT / "templates"

# A template no page or entry draws, with why: once one is drawn, the test
# says so, and its line goes.
DRAWN_BY_NOTHING = {
    "403.html": "Django draws it for a refused request, and the look asks as the estate's owner",
    "404.html": "Django draws it for an address no route answers, which no page of the estate is",
    "500.html": "Django draws it for a request that raised, which no page of the estate does",
    "auth/login.html": "drawn for a reader signed out, and the look signs in as the estate's owner",
    "control_plane/zone_index.html": "drawn only where no zone is managed, and the estate manages one",
    "control_plane/certificate_upload.html": "the estate holds no certificate to upload one for",
    "calendars/entry_confirm_delete.html": "an entry's page is linked only from a day opened beside the month",
    "partials/_attention_list.html": "no template or view draws it; only a test renders it",
    "partials/_row_field.html": "only itself includes it, and no form names it as its table body",
}


def _templates() -> list[str]:
    return sorted(path.relative_to(TEMPLATES).as_posix() for path in TEMPLATES.rglob("*.html"))


@tag("source", "tree")
class EveryTemplateTests(TestCase):
    def test_every_template_reaches_a_page_or_a_catalog_entry(self):
        unreached = {}
        with mock.patch.dict(os.environ, ASKED, clear=True), EstatePages(ASKED["usage_seed"]) as records:
            for template in _templates():
                try:
                    look._pages((f"template:{template}",), records)
                except AssertionError as refused:
                    unreached[template] = str(refused).splitlines()[0]
        self.assertEqual(unreached.keys() - DRAWN_BY_NOTHING.keys(), set(), unreached)
        self.assertEqual(DRAWN_BY_NOTHING.keys() - unreached.keys(), set(), "an exception now reached")
