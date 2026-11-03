"""Schema-to-form projection tests; canonical validation remains authoritative."""

from django.test import SimpleTestCase

from hq.platform.application.capabilities import capability_registry
from hq.platform.application.command_forms import _field_for_schema, command_form_class


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

    def test_a_declared_choice_and_a_model_choice_offer_each_value_by_its_label(self):
        from typing import Annotated

        from hq.platform.application.integration_specs import command_schema
        from hq_sdk.capabilities import Choices, StrictCommand

        class ExampleCommand(StrictCommand):
            tone: Annotated[str, Choices((("plain", "Plain words"), ("terse", "Terse")))]

        declared = _field_for_schema("tone", command_schema(ExampleCommand)["properties"]["tone"], required=True)
        modeled = command_form_class(capability_registry()["asset.create"]).base_fields["payment_method"]

        self.assertEqual(list(declared.choices), [("plain", "Plain words"), ("terse", "Terse")])
        self.assertIn(("", "Not set"), list(modeled.choices))
        self.assertIn(("ach", "ACH / bank transfer"), list(modeled.choices))

    def test_a_label_is_sentence_case_unless_the_command_declares_one(self):
        derived = _field_for_schema("purchase_date", {"type": "string"}, required=False)
        declared = _field_for_schema("ttl", {"type": "integer", "title": "TTL in seconds"}, required=False)

        self.assertEqual(derived.label, "Purchase date")
        self.assertEqual(declared.label, "TTL in seconds")
