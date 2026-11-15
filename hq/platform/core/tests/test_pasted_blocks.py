"""A block HQ draws for a person to paste parses as what HQ meant it to say:
what the Copy button copies from the page, never only what the function
returned, since the page's line breaks are drawn too."""

from importlib import import_module
from importlib.util import find_spec
from unittest import skipUnless

from django.test import TestCase

from hq.platform.application.containers import containers
from hq.platform.application.tests.markup import copied, within
from hq.platform.core.example_estate.testing import throwaway_media
from hq.platform.core.tests.browser.estate import ask_as_production, owner_client

# The module that registers the dense set, loaded now: HQ imports lazily, so
# an import nothing reads would never run it.
import_module("hq.platform.core.tests.test_browser_dense_fixtures")

# The example estate, so an override it comes to raise is held too, and
# the dense set, whose frontend fails every check a compose file can meet.
SETS = ("showcase", "dense")


class ComposeChangeTests(TestCase):
    @skipUnless(find_spec("yaml"), "the image installs no PyYAML")
    def test_every_drawn_compose_change_parses_as_its_changes(self):
        import yaml

        seen = 0
        for name in SETS:
            with self.subTest(set=name), throwaway_media(), owner_client(name) as client:
                for item in containers():
                    hardening = item.hardening
                    if not hardening.yaml:
                        continue
                    meant: dict = {}
                    for change in hardening.changes:
                        written = yaml.safe_load(change.yaml)
                        self.assertIn(change.key, written)
                        self.assertFalse(meant.keys() & written.keys(), change.key)
                        meant.update(written)
                    page = ask_as_production(client, item.url)
                    (block,) = [element for element in within(page, "id", "hardening") if element.tag == "pre"]

                    self.assertEqual(yaml.safe_load(copied(block)), {"services": {hardening.service: meant}}, item.url)
                    seen += 1
        self.assertTrue(seen, "No set raises a compose change")
