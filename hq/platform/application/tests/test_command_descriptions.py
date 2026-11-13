"""Every field of a host command says what it is, wherever it is shown."""

from django.test import SimpleTestCase

from hq.platform.application.capabilities import capability_registry
from hq.platform.application.command_forms import command_form_class
from hq.platform.application.integration_specs import IDEMPOTENCY_FIELD, capability_schema
from hq.platform.application.json_schemas import fields


def host_specs():
    return [spec for spec in capability_registry().values() if spec.command_type.__module__.startswith("hq.")]


class CommandDescriptionTests(SimpleTestCase):
    def test_a_form_shows_each_description_s_meaning_as_its_help(self):
        # The schema says the meaning first, then what only a client reads. A
        # field whose label says all a person needs describes itself to
        # clients alone, and its form shows no help.
        for spec in host_specs():
            properties = fields(capability_schema(spec))
            form = command_form_class(spec)()
            for name, field in properties.items():
                if name in form.fields and name != IDEMPOTENCY_FIELD:
                    with self.subTest(command=spec.name, field=name):
                        help_text = str(form.fields[name].help_text)
                        self.assertTrue(field.get("description", "").startswith(help_text))

    def test_a_description_for_clients_alone_shows_no_help(self):
        from typing import Annotated

        from pydantic import BaseModel

        from hq.platform.application.describe import Describe, meanings

        class Asked(BaseModel):
            hosts: Annotated[str, Describe("", api="Each by its name.")]

        self.assertEqual(meanings(Asked), {"hosts": ""})
        self.assertEqual(Asked.model_json_schema()["properties"]["hosts"]["description"], "Each by its name.")

    def test_every_host_command_field_is_described(self):
        missing = sorted(
            f"{spec.name}.{name}"
            for spec in host_specs()
            for name, field in fields(capability_schema(spec)).items()
            if not str(field.get("description", "")).strip()
        )

        self.assertEqual(missing, [], "Describe each field in its Annotated type with command_fields.Describe(...).")
