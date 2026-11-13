"""The filesystem reading, and what HQ says when a filesystem is nearly full."""

from datetime import UTC

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from hq.domains.control_plane.bridge_contract import contract
from hq.domains.control_plane.observations import OBSERVATIONS

from ..findings import derive_findings
from ..inventory_testing import store
from ..projection import projection_scope
from ..topology import derive_topology
from .test_tailnet_posture import EVERYTHING, raised, tailnet_connection

# Written out rather than imported, so the module loads where the reading is
# not declared yet.
KIND = "host.filesystem"
RULE = "filesystem-nearly-full"
GIB = 1 << 30
MIB = 1 << 20


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


class NearlyFullTests(TestCase):
    def setUp(self):
        tailnet_connection()

    def test_a_root_with_megabytes_left_is_serious(self):
        sweep(filesystem(free=59 * 1000 * 1000), filesystem("/boot", free=800 * MIB, total=GIB))

        (finding,) = raised(RULE)

        self.assertEqual(finding["severity"], "serious")
        self.assertIn("at / on", finding["title"])
        evidence = {item["label"]: item["value"] for item in finding["evidence"]}
        self.assertEqual(set(evidence), {"Free on /"})
        self.assertIn("29.0 GB", evidence["Free on /"])

    def test_under_two_gigabytes_and_a_tenth_is_attention(self):
        sweep(filesystem(free=int(1.5 * GIB)))

        (finding,) = raised(RULE)

        self.assertEqual(finding["severity"], "attention")

    def test_a_small_partition_nearly_full_is_attention(self):
        sweep(filesystem("/boot", free=50 * MIB, total=GIB))

        (finding,) = raised(RULE)

        self.assertEqual(finding["severity"], "attention")

    def test_roomy_filesystems_raise_nothing(self):
        # A small /boot is under the floor but most of it is free, and a large
        # disk is under a tenth free with far more than the floor left.
        sweep(
            filesystem(),
            filesystem("/boot", free=800 * MIB, total=GIB),
            filesystem("/srv", free=300 * GIB, total=4096 * GIB),
        )

        self.assertEqual(raised(RULE), [])

    def test_each_full_filesystem_is_named_in_one_finding(self):
        sweep(filesystem(free=300 * MIB), filesystem("/var/lib/example", free=GIB, total=100 * GIB))

        (finding,) = raised(RULE)

        self.assertEqual(finding["severity"], "serious")
        labels = [item["label"] for item in finding["evidence"]]
        self.assertEqual(labels, ["Free on /", "Free on /var/lib/example"])

    def test_the_steps_show_the_space_then_what_fills_it(self):
        sweep(filesystem(free=59 * 1000 * 1000))

        with projection_scope():
            (finding,) = [
                found
                for found in derive_findings(derive_topology(principal=EVERYTHING), principal=EVERYTHING)
                if found.rule == RULE
            ]

        self.assertTrue(finding.subject)
        self.assertEqual(
            [step.command for step in finding.steps],
            [
                'ssh example-controller "sudo df -h /"',
                'ssh example-controller "sudo du -xh --max-depth=1 / | sort -h | tail"',
            ],
        )

    def test_a_reading_that_failed_raises_nothing_of_its_own(self):
        store(KIND, reachable=False)

        self.assertEqual(raised(RULE), [])
