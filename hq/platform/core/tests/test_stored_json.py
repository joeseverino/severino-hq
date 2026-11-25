"""Every stored value a declared shape reads loads through it, and one the shape refuses is named."""

from io import StringIO

from django.core.management import CommandError, call_command
from django.db import transaction
from django.test import TestCase

from hq.domains.control_plane.models import AdoptedCatalog
from hq.platform.core import seeds
from hq.platform.core.example_estate.testing import throwaway_media


def _checked() -> tuple[str, bool]:
    """What the check says of this database, and whether it refused anything."""

    out = StringIO()
    try:
        call_command("stored_json", stdout=out)
    except CommandError:
        return out.getvalue(), True
    return out.getvalue(), False


class StoredValuesTests(TestCase):
    def test_every_seeded_estate_loads_through_its_declared_shapes(self):
        for name in tuple(seeds.SEEDS):
            with self.subTest(estate=name), throwaway_media(), transaction.atomic():
                with seeds.built(name):
                    said, refused = _checked()
                transaction.set_rollback(True)
                self.assertFalse(refused, said)

    def test_a_catalog_stored_with_fields_its_shape_retired_is_refused_by_name(self):
        with throwaway_media(), seeds.built("standard"):
            catalog = AdoptedCatalog.objects.order_by("-id").first()
            self.assertIsNotNone(catalog)
            stored = [{**procedure, "compose_updates": True, "compose_images": []} for procedure in catalog.procedures]
            AdoptedCatalog.objects.filter(pk=catalog.pk).update(procedures=stored)

            said, refused = _checked()

        self.assertTrue(refused)
        (line,) = [line for line in said.splitlines() if f"/{catalog.pk}/" in line]
        self.assertIn(AdoptedCatalog._meta.db_table, line)
        self.assertIn(f"{AdoptedCatalog._meta.label}.procedures", line)
        self.assertIn("compose_updates", line)
