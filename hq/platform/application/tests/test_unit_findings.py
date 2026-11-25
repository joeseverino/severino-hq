"""The unit state reading, and what HQ says when a unit fails, is absent or stalls."""

import re
from datetime import UTC, timedelta
from pathlib import Path

from django.test import SimpleTestCase, tag
from django.utils import timezone

from hq.domains.control_plane.bridge_contract import contract
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.observations.host import UNIT_KIND

from .. import unit_findings as rules
from ..inventory_testing import store

ROOT = Path(__file__).resolve().parents[4]
UNITS = ROOT / "deploy" / "systemd"
SPEC = OBSERVATIONS[UNIT_KIND]
SERVICE = "severino-hq-example.service"
TIMER = "severino-hq-example.timer"


def stamp(age: timedelta = timedelta(0)) -> str:
    """An instant ``age`` ago, as the controller writes one."""

    return (timezone.now() - age).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def service(unit: str = SERVICE, **said) -> dict:
    """A oneshot that last ran well, as the controller reports it."""

    return {
        "unit": unit, "load": "loaded", "file_state": "static", "active": "inactive", "sub": "dead",
        "result": "success", "main_code": 1, "started_at": stamp(timedelta(minutes=31)),
        "ended_at": stamp(timedelta(minutes=30)), "condition": "yes",
        "condition_at": stamp(timedelta(minutes=31)), "read_at": stamp(), **said,
    }  # fmt: skip


def timer(unit: str = TIMER, activates: str = SERVICE, **said) -> dict:
    """A calendar timer waiting for its next elapse."""

    return {
        "unit": unit, "load": "loaded", "file_state": "enabled", "active": "active", "sub": "waiting",
        "result": "success", "last_trigger_at": stamp(timedelta(minutes=31)),
        "next_elapse_at": stamp(timedelta(hours=-23)), "activates": activates, "read_at": stamp(), **said,
    }  # fmt: skip


def failed(unit: str = SERVICE, **said) -> dict:
    return service(unit, **{"active": "failed", "sub": "failed", "result": "exit-code", "main_status": 3, **said})


def sweep(*records, **fields) -> None:
    """The reading as a sweep stores it, through the schema."""

    kept, refused = SPEC.clean(records)
    assert not refused, records
    store(UNIT_KIND, *kept, **fields)


class RecordTests(SimpleTestCase):
    def test_the_reading_holds_no_secret(self):
        """Every field is a unit name, a state word, a number, a flag or an
        instant, but the unit file's title and what HQ's own program last told
        systemd, which are words, never an assignment, and only the second may
        name a path."""

        declared = contract()["components"]["schemas"]["HostUnitRecord"]
        self.assertFalse(declared["additionalProperties"])
        patterns = {
            name: schema.get("pattern") or schema.get("items", {}).get("pattern")
            for name, schema in declared["properties"].items()
        }
        for name, schema in declared["properties"].items():
            with self.subTest(field=name):
                self.assertTrue(schema["type"] in ("integer", "boolean") or patterns[name], "free text")
                self.assertNotRegex(name, r"exec|environment|credential|path|command")
        words = {"title", "status"}
        self.assertEqual({name for name, pattern in patterns.items() if re.search(pattern or "^$", "two words")}, words)
        for refused, allowed in (
            ("EXAMPLE_TOKEN=sentinel-value", ()),
            ("/run/example/token", {"status"}),
            ("a title\nover two lines", ()),
            ("two words", words),
            ("Sentinel0123456789abcdef0123456789abcdef", words),
        ):
            for name, pattern in patterns.items():
                if pattern and name not in allowed:
                    self.assertIsNone(re.search(pattern, refused), (name, refused))


@tag("source")
class SourceTests(SimpleTestCase):
    """The thresholds are held to the units the repository ships."""

    def seconds(self, setting: str) -> list[timedelta]:
        units = {"s": 1, "sec": 1, "min": 60, "h": 3600, "": 1}
        found = []
        for unit in UNITS.iterdir():
            if not unit.is_file():
                continue
            for value, suffix in re.findall(rf"(?m)^{setting}=(\d+)([a-z]*)$", unit.read_text()):
                found.append(timedelta(seconds=int(value) * units[suffix]))
        return found

    def test_a_waiting_timer_is_allowed_its_randomized_delay(self):
        delays = self.seconds("RandomizedDelaySec")

        self.assertTrue(delays)
        self.assertGreater(rules.OVERDUE_AFTER, max(delays))

    def test_systemd_ends_a_slow_start_before_hq_calls_it_stalled(self):
        bounds = self.seconds("TimeoutStartSec")

        self.assertTrue(bounds)
        self.assertGreater(rules.STARTING_TOO_LONG, max(bounds))

    def test_the_rules_name_no_unit(self):
        source = (ROOT / "hq" / "platform" / "application" / "unit_findings.py").read_text()
        shipped = [
            path.name.split("@")[0].removesuffix(".service").removesuffix(".timer")
            for path in UNITS.iterdir()
            if path.is_file() and not path.name.endswith(".example")
        ]

        self.assertTrue(shipped)
        for name in shipped:
            self.assertNotIn(name, source)

    def test_every_unit_type_shipped_is_judged_kept_active_or_run_when_started(self):
        types = {path.suffix for path in UNITS.iterdir() if path.is_file() and not path.name.endswith(".example")}

        self.assertIn(".service", types)
        for suffix in types:
            with self.subTest(type=suffix):
                self.assertIn(suffix, (*rules.KEPT_ACTIVE, *rules.RUN_WHEN_STARTED))
