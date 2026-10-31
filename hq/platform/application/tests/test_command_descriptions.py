"""Every field of a host command says what it is, wherever it is shown."""

from django.test import SimpleTestCase

from hq.platform.application.capabilities import capability_registry
from hq.platform.application.command_forms import command_form_class
from hq.platform.application.integration_specs import IDEMPOTENCY_FIELD, capability_schema


def host_specs():
    return [spec for spec in capability_registry().values() if spec.command_type.__module__.startswith("hq.")]


class CommandDescriptionTests(SimpleTestCase):
    def test_a_form_shows_each_description_as_its_help(self):
        for spec in host_specs():
            properties = capability_schema(spec).get("properties", {})
            form = command_form_class(spec)()
            for name, field in properties.items():
                if name in form.fields and name != IDEMPOTENCY_FIELD:
                    with self.subTest(command=spec.name, field=name):
                        self.assertEqual(form.fields[name].help_text, field.get("description", ""))

    def test_every_host_command_field_is_described(self):
        missing = sorted(
            f"{spec.name}.{name}"
            for spec in host_specs()
            for name, field in capability_schema(spec).get("properties", {}).items()
            if not str(field.get("description", "")).strip()
        )

        self.assertEqual(missing, [], "Describe each field in its Annotated type with command_fields.Describe(...).")
