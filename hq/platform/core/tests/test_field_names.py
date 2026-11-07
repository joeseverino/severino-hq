"""A model field's name as every reader of it shows it: a form's label, the
API's description and the admin all read its verbose_name."""

from django.apps import apps
from django.test import SimpleTestCase
from django.utils.text import capfirst

from hq.platform.application.labels import field_label


class VerboseNameTests(SimpleTestCase):
    maxDiff = None

    def test_a_fields_name_keeps_each_acronym_in_capitals(self):
        lowered = [
            f"{model._meta.label}.{field.name}: verbose_name={field_label(said)[:1].lower() + field_label(said)[1:]!r}"
            for model in apps.get_models()
            if model.__module__.startswith("hq.")
            for field in model._meta.get_fields()
            if getattr(field, "concrete", False) and not field.auto_created
            if capfirst(said := str(field.verbose_name)) != field_label(said)
        ]

        self.assertEqual(lowered, [])
