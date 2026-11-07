"""The audit log names an object by its model's ``__str__`` (``audit.record_event``,
as Django's admin log does), so every model HQ declares names its rows in words
its owner would use, never Django's "Asset object (12)"."""

from django.apps import apps
from django.db.models import Model
from django.test import SimpleTestCase


def _host_models() -> list[type[Model]]:
    """Every model HQ itself declares, which ``record_event`` can be handed any of.

    An extension names its own models in its own repository.
    """

    return [model for model in apps.get_models() if model.__module__.startswith("hq.")]


class AuditNameTests(SimpleTestCase):
    def test_every_host_model_names_its_rows_itself(self):
        models = _host_models()

        self.assertTrue(models)
        for model in models:
            with self.subTest(model=model._meta.label):
                self.assertIsNot(model.__str__, Model.__str__, "the audit log would show Django's default name")
