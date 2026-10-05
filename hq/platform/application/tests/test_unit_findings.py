"""The unit state reading, and what HQ says when a unit fails, is absent or stalls."""

from __future__ import annotations

import re
from datetime import timedelta, timezone as utc
from pathlib import Path

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from hq.domains.control_plane.bridge_contract import contract
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.observations.host import HostUnitRecord, RENDER_STATUS_KIND, UNIT_KIND

from .. import unit_findings as rules
from ..dashboard import work_queue
from ..findings import derive_findings
from ..inventory_testing import store
from ..projection import projection_scope
from ..topology import derive_topology
from .test_tailnet_posture import EVERYTHING, raised, tailnet_connection

ROOT = Path(__file__).resolve().parents[4]
UNITS = ROOT / "deploy" / "systemd"
SPEC = OBSERVATIONS[UNIT_KIND]
RULES = tuple(rule.name for rule in rules.RULES)
SERVICE = "severino-hq-example.service"
TIMER = "severino-hq-example.timer"


def stamp(age: timedelta = timedelta(0)) -> str:
    """An instant ``age`` ago, as the controller writes one."""

    return (timezone.now() - age).astimezone(utc.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def every_rule() -> dict[str, list]:
    return {name: raised(name) for name in RULES}


NOTHING = {name: [] for name in RULES}


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
        """Every field is a unit name, a state word, a number or an instant."""

        declared = contract()["components"]["schemas"]["HostUnitRecord"]
        self.assertFalse(declared["additionalProperties"])
        for name, schema in declared["properties"].items():
            with self.subTest(field=name):
                self.assertTrue(schema["type"] == "integer" or schema.get("pattern"), "free text")
                self.assertNotRegex(name, r"exec|environment|credential|path|command")
        for sentinel in ("EXAMPLE_TOKEN=sentinel-value", "/run/example/token", "two words", "Sentinel0123456789abcdef0123456789abcdef"):
            for name, schema in declared["properties"].items():
                if "pattern" in schema:
                    self.assertIsNone(re.search(schema["pattern"], sentinel), (name, sentinel))


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
        shipped = [path.name.split("@")[0].removesuffix(".service").removesuffix(".timer")
                   for path in UNITS.iterdir() if path.is_file() and not path.name.endswith(".example")]

        self.assertTrue(shipped)
        for name in shipped:
            self.assertNotIn(name, source)


class HealthyTests(TestCase):
    def setUp(self):
        tailnet_connection()

    def test_units_that_run_raise_nothing(self):
        sweep(
            service(),
            timer(),
            service("severino-hq-example-job@example.work.service"),
            # The controller, read by its own run, is always starting.
            service("severino-hq-example-controller.service", active="activating", sub="start", started_at=stamp()),
            timer("severino-hq-example-controller.timer", "severino-hq-example-controller.service",
                  sub="running", next_elapse_at=""),
            # A unit that has not run on this boot has no instants.
            service("severino-hq-example-later.service", started_at="", ended_at="", condition=""),
        )  # fmt: skip

        self.assertEqual(every_rule(), NOTHING)

    def test_a_machine_that_reports_no_units_raises_nothing(self):
        self.assertEqual(every_rule(), NOTHING)


class FailedTests(TestCase):
    def setUp(self):
        tailnet_connection()

    def test_a_failed_unit_is_serious_and_says_how_it_ended(self):
        sweep(failed(ended_at=stamp(timedelta(hours=5))), timer(), service("severino-hq-example-other.service"))

        (finding,) = raised("unit-failed")

        self.assertEqual(finding["severity"], "serious")
        self.assertIn("1 unit failed on", finding["title"])
        self.assertIn(SERVICE, finding["title"])
        evidence = {item["label"]: item["value"] for item in finding["evidence"]}
        self.assertEqual(evidence["Unit"], SERVICE)
        self.assertEqual(evidence["Last run"], "exit-code, status 3")
        self.assertEqual(evidence["Failed"], "5\xa0hours ago")
        self.assertEqual({name: len(found) for name, found in every_rule().items() if found}, {"unit-failed": 1})

    def test_every_failed_unit_on_a_machine_is_one_finding(self):
        sweep(failed(), failed(TIMER, result="resources"), failed("severino-hq-example-job@example.work.service"))

        (finding,) = raised("unit-failed")

        self.assertIn("3 units failed", finding["title"])

    def test_the_commands_name_the_unit(self):
        sweep(failed())

        with projection_scope():
            (finding,) = [
                found
                for found in derive_findings(derive_topology(principal=EVERYTHING), principal=EVERYTHING)
                if found.rule == "unit-failed"
            ]

        self.assertEqual(
            [step.command for step in finding.steps],
            [f"sudo journalctl -u {SERVICE} -n 50 --no-pager", f"sudo systemctl restart {SERVICE}"],
        )

    def test_a_renderer_that_says_why_it_failed_is_one_finding_not_two(self):
        from .test_render_status import read, sweep as sweep_render

        unit = "severino-hq-secrets.service"
        sweep(failed(unit), failed())
        sweep_render(read(outcome="failed", failure="host"))

        (finding,) = raised("unit-failed")

        self.assertEqual(len(raised("render-failing")), 1)
        self.assertNotIn(unit, repr(finding))
        self.assertIn(SERVICE, finding["title"])

    def test_a_renderer_that_failed_before_it_could_say_so_is_the_units_to_report(self):
        from .test_render_status import read, sweep as sweep_render

        unit = "severino-hq-secrets.service"
        sweep(failed(unit))
        sweep_render(read())

        (finding,) = raised("unit-failed")

        self.assertIn(unit, finding["title"])
        self.assertEqual(raised("render-failing"), [])
        self.assertEqual(OBSERVATIONS[RENDER_STATUS_KIND].kind, RENDER_STATUS_KIND)


class AbsentTests(TestCase):
    def setUp(self):
        tailnet_connection()

    def state(self) -> dict[str, str]:
        (finding,) = raised("unit-not-installed")
        self.assertEqual(finding["severity"], "serious")
        values = [item["value"] for item in finding["evidence"]]
        return dict(zip(values[::2], values[1::2]))

    def test_each_way_a_shipped_unit_is_not_running_is_said(self):
        sweep(
            service(),
            {"unit": "severino-hq-example-gone.service", "load": "not-found", "active": "inactive",
             "sub": "dead", "read_at": stamp()},
            service("severino-hq-example-masked.service", load="masked", file_state="masked"),
            timer("severino-hq-example-off.timer", file_state="disabled", active="inactive", sub="dead"),
            timer("severino-hq-example-idle.timer", active="inactive", sub="dead"),
            {**timer("severino-hq-example-idle.path"), "sub": "waiting", "active": "inactive"},
            timer(),
        )  # fmt: skip

        self.assertEqual(
            self.state(),
            {
                "severino-hq-example-gone.service": "not installed (not-found)",
                "severino-hq-example-masked.service": "not installed (masked)",
                "severino-hq-example-off.timer": "not enabled (disabled)",
                "severino-hq-example-idle.timer": "not started (inactive)",
                "severino-hq-example-idle.path": "not started (inactive)",
            },
        )
        self.assertEqual(raised("timer-stalled"), [])

    def test_a_service_that_is_not_running_now_is_not_absent(self):
        sweep(service(), service("severino-hq-example-other.service", file_state=""))

        self.assertEqual(raised("unit-not-installed"), [])

    def test_a_failed_timer_is_failed_and_not_also_absent(self):
        sweep(failed(TIMER, file_state="enabled"))

        self.assertEqual(raised("unit-not-installed"), [])
        self.assertEqual(len(raised("unit-failed")), 1)


class StalledTests(TestCase):
    def setUp(self):
        tailnet_connection()

    def why(self) -> str:
        (finding,) = raised("timer-stalled")
        self.assertEqual(finding["severity"], "serious")
        self.assertIn(TIMER, finding["title"])
        return {item["label"]: item["value"] for item in finding["evidence"]}["Why"]

    def test_a_timer_with_nothing_scheduled(self):
        sweep(service(), timer(sub="elapsed", next_elapse_at=""))

        self.assertEqual(self.why(), "it has elapsed and nothing is scheduled")

    def test_a_timer_behind_its_own_next_elapse_when_it_was_read(self):
        within = rules.OVERDUE_AFTER - timedelta(minutes=1)
        sweep(service(), timer(next_elapse_at=stamp(within)))
        self.assertEqual(raised("timer-stalled"), [])

        sweep(service(), timer(next_elapse_at=stamp(rules.OVERDUE_AFTER + timedelta(minutes=1))))

        self.assertEqual(self.why(), "it was due more than 2\xa0hours before it was read")

    def test_a_reading_that_has_grown_old_does_not_make_a_timer_late(self):
        # Due an hour after it was read, and read a day ago: the timer was on
        # time when systemd was asked, which is all the reading knows.
        day = timedelta(days=1)
        sweep(service(read_at=stamp(day)), timer(read_at=stamp(day), next_elapse_at=stamp(day - timedelta(hours=1))))

        self.assertEqual(every_rule(), NOTHING)

    def test_a_start_skipped_because_its_condition_does_not_hold(self):
        sweep(service(condition="no", started_at="", ended_at=""), timer())

        self.assertEqual(self.why(), f"{SERVICE} was skipped: its condition was not met")

    def test_a_unit_not_started_since_boot_was_not_skipped(self):
        """systemd says ``ConditionResult=no`` of a unit it has not started yet, with no time."""

        waiting = service(condition="no", condition_at="", started_at="", ended_at="", main_code=0)
        sweep(waiting, timer(last_trigger_at=""))

        self.assertEqual(every_rule(), NOTHING)

    def test_a_timer_on_a_monotonic_schedule_has_no_next_elapse_and_is_not_late(self):
        sweep(service(), timer(next_elapse_at=""), timer("example-b.timer", sub="running", next_elapse_at=""))

        self.assertEqual(every_rule(), NOTHING)

    def test_a_run_that_never_ends(self):
        running = {"active": "activating", "sub": "start"}
        sweep(service(**running, started_at=stamp(timedelta(hours=1))), timer(sub="running", next_elapse_at=""))
        self.assertEqual(raised("timer-stalled"), [])

        sweep(service(**running, started_at=stamp(timedelta(hours=3))), timer(sub="running", next_elapse_at=""))

        self.assertEqual(self.why(), f"{SERVICE} had been starting for more than 2\xa0hours")


class UnreadTests(TestCase):
    def setUp(self):
        tailnet_connection()

    def test_a_reading_the_controller_could_not_take(self):
        store(UNIT_KIND, reachable=False, error="no unit state was mounted")

        (finding,) = raised("unit-state-unread")

        self.assertEqual(finding["severity"], "attention")
        self.assertEqual(finding["evidence"][0]["value"], "the controller could not take the reading")
        self.assertNotIn("mounted", repr(finding["evidence"]))

    def test_a_record_the_schema_refused_is_said_beside_the_units_that_were_read(self):
        sweep(failed(), error="1 record did not match the reading's schema")

        self.assertEqual(len(raised("unit-state-unread")), 1)
        self.assertEqual(len(raised("unit-failed")), 1)


class QueueTests(TestCase):
    def test_each_finding_reaches_the_action_queue_and_offers_a_read(self):
        tailnet_connection()
        sweep(
            failed(),
            timer(sub="elapsed", next_elapse_at=""),
            timer("severino-hq-example-off.timer", active="inactive", sub="dead"),
            error="1 record did not match the reading's schema",
        )

        with projection_scope():
            keys = {item["key"] for item in work_queue()}
            found = {
                finding.rule: finding.subject
                for finding in derive_findings(derive_topology(principal=EVERYTHING), principal=EVERYTHING)
                if finding.rule in RULES
            }

        self.assertEqual(set(found), set(RULES))
        for rule, subject in found.items():
            with self.subTest(rule=rule):
                self.assertIn(f"hq.infrastructure:finding:{rule}:{subject}", keys)
        for finding in raised("unit-failed"):
            verify = [
                action for step in finding["workflow"]["steps"] for action in step["actions"] if action["name"] == "verify"
            ]
            self.assertEqual(len(verify), 1)
            self.assertIn(f"kind={UNIT_KIND}", verify[0]["url"])


class FactTests(SimpleTestCase):
    def test_a_fact_carries_a_unit_whole(self):
        unit = rules.UnitState(
            SERVICE, "loaded", "static", "failed", "failed", "exit-code", "3", "2026-01-02T03:05:00Z",
            "2026-01-02T03:06:00Z", "yes", "2026-01-02T03:05:00Z", "2026-01-03T03:05:00Z", TIMER,
            "2026-01-02T04:00:00Z", "",
        )

        key, value = unit.fact

        self.assertEqual(key, rules.UNIT_STATE)
        self.assertEqual(rules.UnitState.of(value), unit)
