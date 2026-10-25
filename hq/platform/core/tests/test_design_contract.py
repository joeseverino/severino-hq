"""What docs/Design.md tells an extension author to write is what the public contract allows."""

import re
from pathlib import Path

from django.template.defaultfilters import register as django_filters
from django.test import SimpleTestCase, tag

from hq_sdk import template_contract

ROOT = Path(__file__).resolve().parents[4]


@tag("source")
class DesignGuideTests(SimpleTestCase):
    def test_every_template_filter_the_guide_shows_is_public_or_djangos(self):
        guide = (ROOT / "docs" / "Design.md").read_text()
        shown = set(re.findall(r"`[^`\n]*?\|([a-z_]+)", guide))
        self.assertTrue(shown)
        outside = shown - set(template_contract.FILTERS) - set(django_filters.filters)
        self.assertEqual(outside, set(), "filters the guide shows that an extension's template may not use")
