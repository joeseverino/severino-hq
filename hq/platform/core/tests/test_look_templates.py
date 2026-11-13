"""Every template HQ holds is one dev:look can be asked for by name, so the
survey a change to it owes can always run: each reaches a page the estate
links to, a component catalog entry, or a page ``all`` or a bare route name
reaches."""

import os
from unittest import mock

from django.test import TestCase, tag

from hq.platform.application.tests.reads import reads
from hq.platform.core.tests import look
from hq.platform.core.tests.look_records import EstatePages
from hq.platform.core.tests.task import ROOT

ASKED = {"usage_pages": "", "usage_seed": "showcase"}
TEMPLATES = ROOT / "templates"


def _templates() -> list[str]:
    return sorted(path.relative_to(TEMPLATES).as_posix() for path in TEMPLATES.rglob("*.html"))


@tag("source", "tree")
@reads(
    "**/templates/**",
    "static/**",
    "hq/domains/control_plane/bridge_base.json",
    "hq/platform/api/openapi-examples.json",
    "hq/platform/core/browser_audit.js",
    "mise-tasks/**",
    "mise*.toml",
    "mise.lock",
    "tools/pipeline/toolchain.env",
    "controller/go.mod",
    lists=("**/*.py", "**/locale/**"),
)
class EveryTemplateTests(TestCase):
    def test_every_template_reaches_a_page_or_a_catalog_entry(self):
        unreached = {}
        with mock.patch.dict(os.environ, ASKED, clear=True), EstatePages(ASKED["usage_seed"]) as records:
            for template in _templates():
                try:
                    look._pages((f"template:{template}",), records)
                except AssertionError as refused:
                    unreached[template] = str(refused).splitlines()[0]
        self.assertEqual(unreached.keys() - look.DRAWN_BY_NOTHING.keys(), set(), unreached)
        self.assertEqual(look.DRAWN_BY_NOTHING.keys() - unreached.keys(), set(), "an exception now reached")
