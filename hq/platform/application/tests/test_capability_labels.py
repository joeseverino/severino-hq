"""A command is called by what its button says and what it acts on."""

from django.test import SimpleTestCase

from ..capabilities import capability_registry


class EditLabelTests(SimpleTestCase):
    def test_an_infrastructure_records_edit_is_called_by_its_button_and_the_record(self):
        spec = capability_registry()["infrastructure.resource.update"]

        self.assertEqual(spec.label.split()[0], spec.verb)
        self.assertIn(spec.target_label.lower(), spec.label)
