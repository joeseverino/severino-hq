"""Schema-to-form projection tests; canonical validation remains authoritative."""

import re
from dataclasses import dataclass

from django.test import SimpleTestCase

from hq.platform.application.capabilities import capability_registry, capability_spec, command_spec
from hq.platform.application.command_forms import _field_for_schema, command_form_class
from hq.platform.application.contracts import Closed
from hq.platform.application.json_schemas import fields


class CapabilityCommandFormTests(SimpleTestCase):
    def test_destructive_and_infrastructure_effects_require_human_confirmation(self):
        for name in ("project.delete", "certificate.renew"):
            with self.subTest(name=name):
                form_class = command_form_class(command_spec(name))
                self.assertIn("__confirm_effect", form_class.base_fields)
                self.assertTrue(form_class.base_fields["__confirm_effect"].required)

    def test_ordinary_write_commands_do_not_invent_confirmation(self):
        form_class = command_form_class(command_spec("project.create"))

        self.assertNotIn("__confirm_effect", form_class.base_fields)

    def test_a_declared_choice_and_a_model_choice_offer_each_value_by_its_label(self):
        from typing import Annotated

        from hq.platform.application.integration_specs import command_schema
        from hq_sdk.capabilities import Choices

        @dataclass(frozen=True, slots=True)
        class ExampleCommand:
            tone: Annotated[str, Choices((("plain", "Plain words"), ("terse", "Terse")))]

        declared = _field_for_schema("tone", command_schema(ExampleCommand)["properties"]["tone"], required=True)
        modeled = command_form_class(command_spec("asset.create")).base_fields["payment_method"]

        self.assertEqual(list(declared.choices), [("plain", "Plain words"), ("terse", "Terse")])
        self.assertIn(("", "Not set"), list(modeled.choices))
        self.assertIn(("ach", "ACH / bank transfer"), list(modeled.choices))

    def test_a_field_s_help_is_its_meaning_and_the_schema_adds_what_a_client_needs(self):
        from datetime import date
        from typing import Annotated

        from hq.platform.application.integration_specs import CapabilitySpec, command_schema
        from hq_sdk.capabilities import Describe

        type ExampleChecked = Annotated[date | None, Describe("When it was checked.", api="Null for never.")]

        @dataclass(frozen=True, slots=True)
        class ExampleCommand:
            checked: ExampleChecked = None
            owner: Annotated[str | None, Describe("Who keeps it.", api="Their slug; null for nobody.")] = None
            # A use's own marker over its alias's, as the schema says it.
            reviewed: Annotated[ExampleChecked, Describe("When it was reviewed.", api="Null for never.")] = None
            plain: Annotated[str, Describe("Anything else.")] = ""

        spec = CapabilitySpec(
            "example.check.run", "Check it.", "remote_write", "example.write", ExampleCommand, lambda command, **_: {}
        )
        form = command_form_class(spec).base_fields
        said = {name: field.get("description") for name, field in fields(command_schema(ExampleCommand)).items()}

        self.assertEqual(
            {name: form[name].help_text for name in said},
            {
                "checked": "When it was checked.",
                "owner": "Who keeps it.",
                "reviewed": "When it was reviewed.",
                "plain": "Anything else.",
            },
        )
        self.assertEqual(
            said,
            {
                "checked": "When it was checked. Null for never.",
                "owner": "Who keeps it. Their slug; null for nobody.",
                "reviewed": "When it was reviewed. Null for never.",
                "plain": "Anything else.",
            },
        )

    def test_no_form_help_says_what_only_a_client_reads(self):
        # Every registered command's form, an extension's in the composed suite.
        client_words = re.compile(r"`|\bnull\b|ISO 8601|\bempty (?:when|for)\b")
        said = [
            f"{spec.name}.{field_name}: {field.help_text}"
            for spec in sorted(capability_registry(), key=lambda spec: spec.name)
            for field_name, field in command_form_class(spec).base_fields.items()
            if client_words.search(str(field.help_text or ""))
        ]

        self.assertEqual(
            said,
            [],
            "A form's help is the field's meaning in page words: put what only a client reading JSON needs "
            "in the marker's second part, Describe(meaning, api=...), and a closed set or a date's format "
            "in the field's type.",
        )

    def test_no_form_draws_null_or_a_known_shape_as_json(self):
        # Every registered command's form, an extension's in the composed suite,
        # and a procedure's run with inputs of each shape, as an adopted catalog
        # supplies them.
        from django import forms

        from hq.platform.application.command_fields import UPLOAD
        from hq.platform.application.command_forms import _one_type
        from hq.platform.application.integration_specs import command_schema

        from .procedures import Supply, manifest

        class Nothing(Closed):
            pass

        class Window(Closed):
            start: str

        class Maintenance(Closed):
            window: Window

        Supply(
            self,
            manifest(),
            manifest(id="example.nothing", input_type=Nothing),
            manifest(id="example.maintenance", input_type=Maintenance),
        )

        def declared(schema, name):
            """The schema of the field a form field is drawn for, by its name inside any object."""

            definitions = schema.get("$defs", {})
            for part in name.split("."):
                schema = _one_type(fields({"properties": schema.get("properties", {}), "$defs": definitions})[part])
            return schema

        def closed(schema):
            return (
                schema.get("type") == "object"
                and isinstance(schema.get("properties"), dict)
                and not schema.get("additionalProperties")
                and not schema.get(UPLOAD)
            )

        shown, as_json = [], []
        for spec in capability_registry():
            schema = command_schema(spec.command_type)
            for field in command_form_class(spec)():
                if field.value() == "null":
                    shown.append(f"{spec.name}: {field.name}")
                if isinstance(field.field, forms.JSONField) and closed(declared(schema, field.name)):
                    as_json.append(f"{spec.name}: {field.name}")

        self.assertIsNotNone(capability_spec("automation.example.maintenance.run"))
        self.assertEqual(shown, [], "A field holding nothing is drawn blank, never as JSON's null.")
        self.assertEqual(
            as_json, [], "An object whose schema names each of its fields is drawn as those fields, never as JSON."
        )

    def test_a_field_of_a_shared_alias_is_labeled_from_its_own_name(self):
        from datetime import date
        from typing import Annotated

        from pydantic import Field

        from hq.platform.application.integration_specs import CapabilitySpec
        from hq_sdk.capabilities import Describe

        # The alias's definition is titled from the alias's name, for a viewer.
        type ExampleLastChecked = Annotated[date | None, Describe("When it was checked.")]

        @dataclass(frozen=True, slots=True)
        class ExampleCommand:
            reviewed_on: ExampleLastChecked = None
            ttl: Annotated[int, Field(le=86400, title="TTL in seconds"), Describe("How long it lasts.")] = 60

        spec = CapabilitySpec(
            "example.check.run", "Check it.", "remote_write", "example.write", ExampleCommand, lambda command, **_: {}
        )
        form = command_form_class(spec).base_fields

        self.assertEqual(form["reviewed_on"].label, "Reviewed on")
        self.assertEqual(form["ttl"].label, "TTL in seconds")

    def test_a_field_for_agents_is_left_off_the_form_and_one_it_must_be_sent_is_refused(self):
        from typing import Annotated

        from hq.platform.application.command_fields import ForAgents
        from hq.platform.application.integration_specs import CapabilitySpec

        @dataclass(frozen=True, slots=True)
        class ExampleCommand:
            note: str = ""
            trace: Annotated[str, ForAgents()] = ""

        @dataclass(frozen=True, slots=True)
        class ExampleAsked:
            trace: Annotated[str, ForAgents()]

        def spec(command):
            return CapabilitySpec("example.check.run", "Check it.", "remote_write", "example.write", command, dict)

        self.assertEqual(set(command_form_class(spec(ExampleCommand)).payload_names), {"note"})
        with self.assertRaises(ValueError):
            command_form_class(spec(ExampleAsked))

    def test_a_file_field_is_picked_as_a_file_and_sent_as_the_command_takes_it(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        form_class = command_form_class(command_spec("receipt.upload"))
        form = form_class(
            {"amount": "1.00"},
            {"file": SimpleUploadedFile("example.pdf", b"%PDF-1.4", content_type="application/pdf")},
        )

        self.assertTrue(form_class().is_multipart())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.command_payload["file"],
            {"name": "example.pdf", "content_type": "application/pdf", "content": "JVBERi0xLjQ="},
        )

    def test_a_label_is_sentence_case_unless_the_command_declares_one(self):
        derived = _field_for_schema("purchase_date", {"type": "string"}, required=False)
        declared = _field_for_schema("ttl", {"type": "integer", "title": "TTL in seconds"}, required=False)

        self.assertEqual(derived.label, "Purchase date")
        self.assertEqual(declared.label, "TTL in seconds")

    def test_a_list_is_posted_a_row_an_item_and_read_back_as_its_item_type(self):
        from typing import Annotated

        from django.utils.datastructures import MultiValueDict
        from pydantic import Field

        from hq.platform.application.integration_specs import CapabilitySpec

        @dataclass(frozen=True, slots=True)
        class ExampleCommand:
            ports: list[Annotated[int, Field(le=65535)]]

        spec = CapabilitySpec("example.ports.set", "Set them.", "remote_write", "example.write", ExampleCommand, dict)
        form = command_form_class(spec, fields=("ports",))(MultiValueDict({"ports": ["80", "443", ""]}))

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.command_payload["ports"], [80, 443])
