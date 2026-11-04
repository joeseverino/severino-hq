"""A GitHub repository the controller found unchanged keeps the record HQ holds."""

from datetime import UTC, datetime

from django.test import TestCase

from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.observations.github import REPOSITORY_KIND

from ..inventory import record_inventory
from ..security import cli_principal

READ_AT = datetime(2030, 1, 2, 12, 0, tzinfo=UTC)


def read(name, *, fingerprint="f1", sha="abc"):
    return {
        "connection_ref": "github",
        "repository": name,
        "default_branch": "main",
        "head": {"sha": sha},
        "fingerprint": fingerprint,
        "read_at": READ_AT.isoformat(),
        "carried": False,
    }


def carried(name):
    return {"connection_ref": "github", "repository": name, "carried": True}


def sweep(*records):
    record_inventory({REPOSITORY_KIND: {"ok": True, "records": list(records)}}, principal=cli_principal())
    return {record["repository"]: record for record in ProviderInventory.objects.get(kind=REPOSITORY_KIND).records}


class CarriedRepositoryTests(TestCase):
    def test_a_carried_repository_keeps_the_record_held(self):
        sweep(read("example/alpha"), read("example/beta"))

        stored = sweep(carried("example/alpha"), read("example/beta", fingerprint="f2", sha="def"))

        self.assertEqual(stored["example/alpha"]["head"], {"sha": "abc"})
        self.assertEqual(stored["example/alpha"]["fingerprint"], "f1")
        self.assertEqual(
            (stored["example/beta"]["head"], stored["example/beta"]["fingerprint"]), ({"sha": "def"}, "f2")
        )
        self.assertFalse(any(record.get("carried") for record in stored.values()))

    def test_a_carried_repository_hq_holds_no_record_of_is_not_stored(self):
        sweep(read("example/alpha"))

        stored = sweep(read("example/alpha"), carried("example/unknown"))

        self.assertEqual(set(stored), {"example/alpha"})
