"""A field's effect is said once."""

from __future__ import annotations

from django.test import SimpleTestCase

from hq.domains.control_plane.providers import PROVIDERS


class ChangeEffectTests(SimpleTestCase):
    def test_a_field_does_not_repeat_its_declared_effect(self):
        # The form prints a provider's change effect under the field it names,
        # so a description that also says it prints the sentence twice.
        repeated = []
        for kind, provider in PROVIDERS.items():
            properties = provider.schema().get("properties", {})
            for name, effect in provider.change_effects:
                description = properties.get(name, {}).get("description", "")
                if effect in description or description and description in effect:
                    repeated.append(f"{kind}.{name}")
        self.assertEqual(repeated, [])

    def test_every_effect_names_a_field_that_exists(self):
        missing = [
            f"{kind}.{name}"
            for kind, provider in PROVIDERS.items()
            for name, _ in provider.change_effects
            if name not in provider.schema().get("properties", {})
        ]
        self.assertEqual(missing, [])
