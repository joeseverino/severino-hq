"""Schema-to-form projection tests; canonical validation remains authoritative."""

from django.test import SimpleTestCase

from application.capabilities import capability_registry
from application.command_forms import command_form_class


class CapabilityCommandFormTests(SimpleTestCase):
    def test_destructive_and_infrastructure_effects_require_human_confirmation(self):
        registry = capability_registry()
        for name in ("project.delete", "certificate.renew"):
            with self.subTest(name=name):
                form_class = command_form_class(registry[name])
                self.assertIn("__confirm_effect", form_class.base_fields)
                self.assertTrue(form_class.base_fields["__confirm_effect"].required)

    def test_ordinary_write_commands_do_not_invent_confirmation(self):
        form_class = command_form_class(capability_registry()["project.create"])

        self.assertNotIn("__confirm_effect", form_class.base_fields)

    def test_browser_execution_key_is_strict_and_bounded(self):
        form_class = command_form_class(capability_registry()["project.create"])
        form = form_class(
            {
                "name": "Example",
                "__execution_key": "contains a space",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("__execution_key", form.errors)
