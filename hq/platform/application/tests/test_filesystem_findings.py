"""The filesystem reading, and what HQ says when a filesystem is nearly full."""

from datetime import UTC

from django.test import SimpleTestCase
from django.utils import timezone

from hq.domains.control_plane.bridge_contract import contract
from hq.domains.control_plane.observations import OBSERVATIONS

from ..inventory_testing import store

# Written out rather than imported, so the module loads where the reading is
# not declared yet.
KIND = "host.filesystem"
GIB = 1 << 30


def filesystem(mount: str = "/", free: int = 10 * GIB, total: int = 29 * GIB, **said) -> dict:
    """One filesystem as the controller reports it."""

    read_at = timezone.now().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"mount": mount, "free_bytes": free, "total_bytes": total, "read_at": read_at, **said}


def sweep(*records, **fields) -> None:
    """The reading as a sweep stores it, through the schema."""

    kept, refused = OBSERVATIONS[KIND].clean(records)
    assert not refused, records
    store(KIND, *kept, **fields)


class RecordTests(SimpleTestCase):
    def test_the_record_is_the_contracts(self):
        from hq.domains.control_plane.observations.host import HostFilesystemRecord

        declared = contract()["components"]["schemas"]["HostFilesystemRecord"]

        self.assertEqual(set(HostFilesystemRecord.model_fields), set(declared["properties"]))

    def test_a_device_or_its_options_are_never_kept(self):
        record = filesystem(device="/dev/sda2", options="rw,relatime")

        (kept,), refused = OBSERVATIONS[KIND].clean([record])

        self.assertEqual(refused, 0)
        self.assertNotIn("device", kept)
        self.assertNotIn("options", kept)

    def test_a_mount_that_is_not_a_path_is_refused(self):
        _kept, refused = OBSERVATIONS[KIND].clean([filesystem(mount="relative"), filesystem(mount="/a|b")])

        self.assertEqual(refused, 2)
