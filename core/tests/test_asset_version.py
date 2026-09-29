"""A served asset's URL changes when it can change, and never otherwise."""

from __future__ import annotations

from django.test import SimpleTestCase, override_settings

from core.context_processors import _asset_version


class AssetVersionTests(SimpleTestCase):
    @override_settings(DEBUG=False, STATIC_LIVE=True)
    def test_a_live_server_with_debug_off_does_not_pin_its_first_fingerprint(self):
        # Pinned, every edit after start reached the browser under a URL it
        # already held as immutable.
        self.assertTrue(_asset_version().startswith("dev"))

    @override_settings(DEBUG=False, STATIC_LIVE=False)
    def test_production_names_assets_by_their_content(self):
        self.assertFalse(_asset_version().startswith("dev"))


class ReadoutTests(SimpleTestCase):
    def test_an_identifier_is_code_and_prose_is_text(self):
        from core.templatetags.value_tags import readout

        for value in ("tcp:22", "example-box", "example.com", "op://vault/item"):
            with self.subTest(value=value):
                self.assertEqual(str(readout(value)), f"<code>{value}</code>")
        for value in (11, "On", "Need approval", "180 days"):
            with self.subTest(value=value):
                self.assertEqual(str(readout(value)), str(value))

    def test_text_is_escaped_either_way(self):
        from django.template import Context, Template

        rendered = Template("{{ value|readout }}").render(Context({"value": "<b>x</b>"}))
        self.assertNotIn("<b>", rendered)
