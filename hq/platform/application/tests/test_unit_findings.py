"""The unit state reading, and what HQ says when a unit fails, is absent or stalls."""

import re
from datetime import UTC, timedelta
from pathlib import Path

from django.test import SimpleTestCase, tag
from django.utils import timezone

from hq.domains.control_plane.bridge_contract import contract
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.observations.host import UNIT_KIND, HostUnitRecord

from .. import unit_findings as rules
from ..inventory_testing import store

ROOT = Path(__file__).resolve().parents[4]
UNITS = ROOT / "deploy" / "systemd"
SPEC = OBSERVATIONS[UNIT_KIND]
RULES = tuple(rule.name for rule in rules.RULES)
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


def socket(unit: str, activates: str = SERVICE, **said) -> dict:
    """A unit that does its work while active, a socket listening by default."""

    return {
        "unit": unit, "load": "loaded", "file_state": "enabled", "active": "active", "sub": "listening",
        "activates": activates, "read_at": stamp(), **said,
    }  # fmt: skip


def failed(unit: str = SERVICE, **said) -> dict:
    return service(unit, **{"active": "failed", "sub": "failed", "result": "exit-code", "main_status": 3, **said})


FOREIGN = "example-upload.service"


def looping(unit: str = FOREIGN, **said) -> dict:
    """A service HQ does not ship that systemd keeps restarting, waiting out its
    RestartSec, as the controller reports one."""

    return {
        "unit": unit, "title": "Example upload", "load": "loaded", "file_state": "disabled",
        "active": "activating", "sub": "auto-restart", "result": "exit-code", "main_code": 1,
        "main_status": 1, "restarts": 4127, "active_at": stamp(timedelta(minutes=1)),
        "ended_at": stamp(timedelta(seconds=30)), "foreign": True, "read_at": stamp(), **said,
    }  # fmt: skip


def sweep(*records, **fields) -> None:
    """The reading as a sweep stores it, through the schema."""

    kept, refused = SPEC.clean(records)
    assert not refused, records
    store(UNIT_KIND, *kept, **fields)


class RecordTests(SimpleTestCase):
    def test_the_record_is_the_contracts(self):
        declared = contract()["components"]["schemas"]["HostUnitRecord"]

        self.assertEqual(set(HostUnitRecord.model_fields), set(declared["properties"]))
        self.assertEqual(
            {name for name, field in HostUnitRecord.model_fields.items() if field.is_required()},
            set(declared["required"]),
        )

    def test_a_record_is_kept_as_the_controller_sent_it(self):
        records = [failed(), timer()]

        kept, refused = SPEC.clean(records)

        self.assertEqual((kept, refused), (records, 0))

    def test_a_field_the_schema_does_not_name_is_dropped(self):
        record = service()
        record["environment"] = "EXAMPLE_TOKEN=sentinel-value"
        record["exec_start"] = "/usr/local/bin/example --token sentinel-value"

        (kept,), _ = SPEC.clean([record])

        self.assertNotIn("sentinel-value", repr(kept))

    def test_a_string_that_is_not_a_name_a_word_or_an_instant_is_refused(self):
        for field, value in (
            ("unit", "/etc/example/credentials"),
            ("unit", "example.service; id"),
            ("active", "failed with EXAMPLE_TOKEN=sentinel-value"),
            ("result", "Exit Code"),
            ("ended_at", "yesterday afternoon"),
            ("activates", "sentinel-value"),
            ("main_status", -1),
        ):
            with self.subTest(field=field, value=value):
                self.assertEqual(SPEC.clean([service(**{field: value})]), ([], 1))

    def test_the_reading_holds_no_secret(self):
        """Every field is a unit name, a state word, a number, a flag or an
        instant, but the unit file's title, which is words and never a path or
        an assignment."""

        declared = contract()["components"]["schemas"]["HostUnitRecord"]
        self.assertFalse(declared["additionalProperties"])
        for name, schema in declared["properties"].items():
            with self.subTest(field=name):
                self.assertTrue(schema["type"] in ("integer", "boolean") or schema.get("pattern"), "free text")
                self.assertNotRegex(name, r"exec|environment|credential|path|command")
        # Description is the one field of words; a second waits on a decision.
        words = {"title"}
        self.assertEqual(
            {
                name
                for name, schema in declared["properties"].items()
                if re.search(schema.get("pattern", "^$"), "two words")
            },
            words,
        )
        for refused, anywhere in (
            ("EXAMPLE_TOKEN=sentinel-value", True),
            ("/run/example/token", True),
            ("a title\nover two lines", True),
            ("two words", False),
            ("Sentinel0123456789abcdef0123456789abcdef", False),
        ):
            for name, schema in declared["properties"].items():
                if "pattern" in schema and (anywhere or name not in words):
                    self.assertIsNone(re.search(schema["pattern"], refused), (name, refused))


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


class FactTests(SimpleTestCase):
    def test_a_fact_carries_a_unit_whole(self):
        unit = rules.UnitState(
            SERVICE,
            "loaded",
            "static",
            "failed",
            "failed",
            "exit-code",
            "3",
            "2026-01-02T03:05:00Z",
            "2026-01-02T03:06:00Z",
            "yes",
            "2026-01-02T03:05:00Z",
            "2026-01-03T03:05:00Z",
            TIMER,
            "2026-01-02T04:00:00Z",
            "",
            "",
            "12",
            "2026-01-02T03:05:30Z",
            "yes",
        )

        key, value = unit.fact

        self.assertEqual(key, rules.UNIT_STATE)
        self.assertEqual(rules.UnitState.of(value), unit)
