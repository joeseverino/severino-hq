"""The secret render reading, and what HQ says when the secrets go stale."""

import re
import unittest
from html import unescape
from datetime import datetime, timedelta, timezone as utc
from pathlib import Path

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, ProviderInventory
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.observations.host import (
    RENDER_REASONS,
    RENDERER_NAME,
    RENDER_STATUS_KIND,
    STATUS_WORD,
    UNREADABLE_WORD,
)

from .. import render_status_findings as rules
from ..dashboard import work_queue
from ..findings import derive_findings
from ..inventory import record_inventory
from ..inventory_testing import store
from ..projection import projection_scope
from ..security import cli_principal, web_principal
from ..topology import derive_topology
from .test_remedy_links import TRAP, UNOFFERED, _command_links, an_operator
from .test_tailnet_posture import EVERYTHING, raised, tailnet_connection

ROOT = Path(__file__).resolve().parents[4]
SPEC = OBSERVATIONS[RENDER_STATUS_KIND]
RULES = tuple(rule.name for rule in rules.RULES)


def stamp(age: timedelta) -> str:
    """An instant ``age`` ago, as the renderer writes one."""

    return (timezone.now() - age).astimezone(utc.utc).isoformat().replace("+00:00", "Z")


def document(
    *,
    outcome: str = "current",
    failure: str = "",
    attempted: timedelta = timedelta(minutes=10),
    confirmed: timedelta | None = timedelta(minutes=10),
    rendered: timedelta = timedelta(hours=5),
    sync: str | None = "ACTIVE",
) -> dict:
    """A status document as ``secretstatus.Status`` marshals it."""

    found: dict = {
        "schema_version": 1,
        "last_attempt": {"at": stamp(attempted), "outcome": outcome, **({"failure": failure} if failure else {})},
    }
    if confirmed is not None:
        found["last_success"] = {
            "at": stamp(confirmed),
            "rendered_at": stamp(max(rendered, confirmed)),
            "content_version": 42,
            "attribute_version": 3,
            "counts": {"items_read": 20, "connections": 9, "app_variables": 31, "identities": 2, "signing_keys": 1},
        }
    if sync is not None:
        found["connect"] = {
            "read_at": stamp(attempted),
            "version": "1.8.1",
            "dependencies": [{"service": "sqlite", "status": "ACTIVE"}, {"service": "sync", "status": sync}],
        }
    return found


def read(renderer: str = "hq", **status) -> dict:
    return {"renderer": renderer, "state": "read", "status": document(**status)}


def unread(reason: str, renderer: str = "hq") -> dict:
    return {"renderer": renderer, "state": "unreadable", "reason": reason}


def sweep(*records, **fields) -> None:
    """The reading as a sweep stores it, through the schema."""

    kept, refused = SPEC.clean(records)
    assert not refused, records
    store(RENDER_STATUS_KIND, *kept, **fields)


def every_rule() -> dict[str, list]:
    return {name: raised(name) for name in RULES}


class RecordTests(SimpleTestCase):
    def test_the_document_is_kept_as_the_renderer_wrote_it(self):
        record = read()

        (kept,), refused = SPEC.clean([record])

        self.assertEqual(refused, 0)
        # The version is the Go type's to check; HQ keeps what it reads.
        del record["status"]["schema_version"]
        self.assertEqual(kept, record)

    def test_a_field_the_schema_does_not_name_is_dropped_at_every_depth(self):
        record = read()
        record["path"] = "/run/example/status.json"
        record["status"]["vault"] = "Example Vault"
        record["status"]["last_attempt"]["detail"] = "Connect said no"
        record["status"]["connect"]["dependencies"][0]["message"] = "Connected to an example database"

        (kept,), _ = SPEC.clean([record])

        for word in ("/run/example", "Example Vault", "Connect said no", "example database"):
            self.assertNotIn(word, repr(kept))

    def test_a_string_that_is_not_a_short_word_is_never_stored(self):
        record = read(outcome="failed", failure="the token was refused by Connect", sync="TOKEN NEEDED\nnow")
        record["status"]["connect"]["version"] = "1.8.1 (example build)"
        record["status"]["last_attempt"]["at"] = "yesterday afternoon"

        (kept,), refused = SPEC.clean([record])

        self.assertEqual(refused, 0)
        status = kept["status"]
        self.assertEqual(status["last_attempt"]["failure"], UNREADABLE_WORD)
        self.assertEqual(status["last_attempt"]["at"], "")
        self.assertEqual(status["connect"]["version"], UNREADABLE_WORD)
        self.assertEqual(status["connect"]["dependencies"][1]["status"], UNREADABLE_WORD)

    def test_a_nanosecond_instant_is_one(self):
        record = read()
        record["status"]["last_attempt"]["at"] = "2026-01-01T00:00:00.123456789Z"

        (kept,), _ = SPEC.clean([record])

        self.assertEqual(kept["status"]["last_attempt"]["at"], "2026-01-01T00:00:00.123456789Z")

    def test_a_record_that_does_not_name_a_renderer_and_a_state_is_refused(self):
        for record in (
            {"state": "read", "status": document()},
            {"renderer": "/run/example/status.json", "state": "unreadable", "reason": "missing"},
            {"renderer": "Example Renderer", "state": "unreadable", "reason": "missing"},
            {"renderer": "hq", "state": "fine"},
            {"renderer": "hq", "state": "unreadable", "reason": "the file was not there"},
            {"renderer": "hq", "state": "read", "status": {"last_attempt": {"at": stamp(timedelta(0))}, "connect": {
                "dependencies": [{"service": "sync", "status": "ACTIVE"}] * 17}}},
        ):
            with self.subTest(record=record):
                self.assertEqual(SPEC.clean([record]), ([], 1))

    def test_an_unreadable_document_is_a_record_with_its_reason(self):
        for reason in RENDER_REASONS:
            with self.subTest(reason=reason):
                self.assertEqual(SPEC.clean([unread(reason)]), ([unread(reason)], 0))


@unittest.skipUnless((ROOT / "controller" / "secretstatus").is_dir(), "the controller's source is not in the image")
class OneDeclarationTests(SimpleTestCase):
    """What HQ restates of the renderer, held to where the renderer declares it."""

    def source(self, *parts: str) -> str:
        return ROOT.joinpath(*parts).read_text(encoding="utf-8")

    def test_the_word_pattern_is_the_renderers(self):
        declared = re.search(r"const WordPattern = `([^`]+)`", self.source("controller", "secretstatus", "status.go"))

        self.assertEqual(declared.group(1), STATUS_WORD)

    def test_the_renderer_name_is_the_controllers(self):
        declared = re.search(
            r"rendererName = regexp\.MustCompile\(`([^`]+)`\)",
            self.source("controller", "providers", "host_readings.go"),
        )

        self.assertEqual(declared.group(1), RENDERER_NAME)

    def test_the_reasons_are_the_controllers(self):
        source = self.source("controller", "providers", "host_readings.go")
        said = set(re.findall(r'unreadable\("(\w+)"\)', source))

        self.assertEqual(said, set(RENDER_REASONS))

    def test_every_failure_class_is_explained(self):
        source = self.source("controller", "secrets", "render.go")
        body = source[source.index("func Class(err error) string") :]
        classes = set(re.findall(r'return "(\w+)"', body[: body.index("\n}\n")]))

        self.assertEqual(classes, set(rules.FAILURE_CLASSES))

    def test_the_thresholds_are_the_timers_and_the_renderers(self):
        timer = self.source("deploy", "systemd", "severino-hq-secrets.timer")
        main = self.source("controller", "cmd", "hq-secrets", "main.go")

        self.assertIn("OnUnitActiveSec=1h\n", timer)
        self.assertIn("RandomizedDelaySec=5min\n", timer)
        self.assertIn("FullEvery:       24 * time.Hour,", main)
        self.assertEqual(rules.RENDER_EVERY, timedelta(hours=1))
        self.assertEqual(rules.RENDER_DELAY, timedelta(minutes=5))
        self.assertEqual(rules.FULL_READ_EVERY, timedelta(hours=24))
        self.assertEqual(rules.CONFIRMED_WITHIN, timedelta(hours=3, minutes=15))
        self.assertEqual(rules.RENDERED_WITHIN, timedelta(hours=27, minutes=15))

    def test_every_renderer_the_launcher_names_has_its_unit(self):
        launcher = self.source("scripts", "run-controller.sh")
        block = launcher[launcher.index("done <<EOF\n") + len("done <<EOF\n") :]
        named = [line.partition("=")[0] for line in block[: block.index("\nEOF\n")].splitlines()]

        self.assertEqual(named, list(rules.RENDERER_UNITS))
        for name, unit in rules.RENDERER_UNITS.items():
            with self.subTest(renderer=name):
                self.assertRegex(name, RENDERER_NAME)
                self.assertTrue((ROOT / "deploy" / "systemd" / unit).is_file())
                self.assertTrue((ROOT / "deploy" / "systemd" / unit.replace(".service", ".timer")).is_file())


class HealthyTests(TestCase):
    def setUp(self):
        tailnet_connection()

    def test_a_current_render_raises_nothing(self):
        sweep(read())

        self.assertEqual(every_rule(), {name: [] for name in RULES})

    def test_a_full_render_just_now_raises_nothing(self):
        sweep(read(outcome="rendered", attempted=timedelta(0), confirmed=timedelta(0), rendered=timedelta(0)))

        self.assertEqual(every_rule(), {name: [] for name in RULES})

    def test_a_reading_no_launcher_names_raises_nothing(self):
        store(RENDER_STATUS_KIND, connected=False)

        self.assertEqual(every_rule(), {name: [] for name in RULES})

    def test_no_reading_raises_nothing(self):
        self.assertEqual(every_rule(), {name: [] for name in RULES})

    def test_two_runs_missed_is_not_yet_stale(self):
        age = rules.CONFIRMED_WITHIN - timedelta(minutes=1)
        sweep(read(attempted=age, confirmed=age))

        self.assertEqual(raised("render-stale"), [])

    def test_a_full_read_a_day_old_is_not_yet_overdue(self):
        sweep(read(rendered=rules.RENDERED_WITHIN - timedelta(minutes=1)))

        self.assertEqual(raised("render-stale"), [])


class FailingTests(TestCase):
    def setUp(self):
        tailnet_connection()

    def test_a_failed_run_names_its_class_and_the_commands(self):
        sweep(read(outcome="failed", failure="connect_denied", confirmed=timedelta(hours=1, minutes=10)))

        (finding,) = raised("render-failing")

        self.assertEqual(finding["title"], "example-controller could not refresh its credentials from 1Password: 1Password Connect refused the token")
        # One failed run on files still inside the allowance.
        self.assertEqual(finding["severity"], "attention")
        self.assertIn({"label": "Reason", "value": "1Password Connect refused the token"}, finding["evidence"])
        self.assertIn({"label": "Job", "value": "hq"}, finding["evidence"])
        self.assertEqual(
            [step["command"] for step in finding["operator_steps"]],
            [
                'ssh example-controller "sudo journalctl -u severino-hq-secrets.service -n 50 --no-pager"',
                'ssh example-controller "sudo systemctl start severino-hq-secrets.service"',
            ],
        )
        self.assertIsNone(finding["no_help_reason"])
        # A failed run is this rule's alone.
        self.assertEqual(raised("render-stale"), [])

    def test_a_failure_that_outlasts_the_allowance_is_serious(self):
        sweep(read(outcome="failed", failure="connect_unavailable", confirmed=timedelta(days=8), rendered=timedelta(days=8)))

        (finding,) = raised("render-failing")

        self.assertEqual(finding["severity"], "serious")
        self.assertIn({"label": "Last refreshed", "value": "1\xa0week, 1\xa0day ago"}, finding["evidence"])
        self.assertEqual(raised("render-stale"), [])

    def test_a_renderer_that_never_succeeded_is_serious(self):
        sweep(read(outcome="failed", failure="config", confirmed=None, sync=None))

        (finding,) = raised("render-failing")

        self.assertEqual(finding["severity"], "serious")
        self.assertIn({"label": "Last refreshed", "value": "never"}, finding["evidence"])
        self.assertEqual(raised("connect-sync-stalled"), [])

    def test_a_class_this_release_does_not_know_is_still_named(self):
        sweep(read(outcome="failed", failure="example_class"))

        (finding,) = raised("render-failing")

        self.assertIn("(example_class)", finding["title"])
        self.assertIn(
            {"label": "Reason", "value": "an error HQ does not recognise (example_class)"}, finding["evidence"]
        )

    def test_a_renderer_with_no_known_unit_gets_the_steps_in_words(self):
        sweep(read("other-apps", outcome="failed", failure="content"))

        (finding,) = raised("render-failing")

        (step,) = finding["operator_steps"]
        self.assertEqual(step["command"], "")
        self.assertIn("job's log", step["label"])
        self.assertEqual(finding["no_help_reason"], "HQ cannot run commands on example-controller.")

    def test_two_renderers_on_one_machine_are_one_finding_naming_both(self):
        sweep(read(outcome="failed", failure="content"), read("other-apps", outcome="failed", failure="busy"))

        (finding,) = raised("render-failing")

        self.assertIn("something in the vault was refused, another refresh was already running", finding["title"])
        renderers = [item["value"] for item in finding["evidence"] if item["label"] == "Job"]
        self.assertEqual(renderers, ["hq", "other-apps"])


class StaleTests(TestCase):
    def setUp(self):
        tailnet_connection()

    def test_a_renderer_that_stopped_running_is_serious(self):
        age = rules.CONFIRMED_WITHIN + timedelta(minutes=1)
        sweep(read(attempted=age, confirmed=age, rendered=age))

        (finding,) = raised("render-stale")

        self.assertEqual(finding["severity"], "serious")
        self.assertIn("example-controller has not refreshed its credentials since ", finding["title"])
        self.assertIn({"label": "Last refreshed", "value": "3\xa0hours, 16\xa0minutes ago"}, finding["evidence"])
        self.assertEqual(
            [step["command"] for step in finding["operator_steps"]],
            [
                'ssh example-controller "sudo systemctl list-timers severino-hq-secrets.timer"',
                'ssh example-controller "sudo journalctl -u severino-hq-secrets.service -n 50 --no-pager"',
                'ssh example-controller "sudo systemctl start severino-hq-secrets.service"',
            ],
        )
        self.assertEqual(raised("render-failing"), [])

    def test_a_full_read_overdue_is_stale_while_every_run_says_current(self):
        sweep(read(rendered=rules.RENDERED_WITHIN + timedelta(minutes=1)))

        (finding,) = raised("render-stale")

        self.assertIn({"label": "Last read in full", "value": "1\xa0day, 3\xa0hours ago"}, finding["evidence"])

    def test_a_reading_taken_hours_ago_while_nobody_looked_raises_nothing(self):
        # Idle, the controller reads half a day apart. The renderer was current
        # when this was read, and has had every hour since to run again.
        age = rules.CONFIRMED_WITHIN + timedelta(hours=2)
        sweep(read(attempted=age, confirmed=age, rendered=age), age=age)

        self.assertEqual(raised("render-stale"), [])

    def test_a_renderer_behind_when_it_was_read_is_still_behind_hours_later(self):
        age = timedelta(hours=2)
        behind = age + rules.CONFIRMED_WITHIN + timedelta(minutes=1)
        sweep(read(attempted=behind, confirmed=behind, rendered=behind), age=age)

        self.assertEqual(len(raised("render-stale")), 1)

    def test_a_reading_the_controller_stopped_taking_goes_stale_against_now(self):
        # The copy the controller last read said "current"; nothing has read
        # one since, and the files it described are a day old.
        age = timedelta(days=1)
        sweep(read(attempted=age, confirmed=age, rendered=age), age=age)

        self.assertEqual(len(raised("render-stale")), 1)


class SyncTests(TestCase):
    def setUp(self):
        tailnet_connection()

    def test_a_sync_that_is_not_active_is_named(self):
        sweep(read(sync="TOKEN_NEEDED"))

        (finding,) = raised("connect-sync-stalled")

        self.assertEqual(finding["severity"], "attention")
        self.assertEqual(finding["title"], "1Password Connect on example-controller has stopped syncing")
        self.assertIn({"label": "Sync", "value": "TOKEN_NEEDED"}, finding["evidence"])
        self.assertEqual(finding["operator_steps"][-1]["command"], 'ssh example-controller "sudo systemctl start severino-hq-secrets.service"')
        # The render itself succeeded, from Connect's cache.
        self.assertEqual(raised("render-failing"), [])
        self.assertEqual(raised("render-stale"), [])

    def test_a_connect_that_reports_no_sync_dependency_is_not_vouched_for(self):
        record = read()
        record["status"]["connect"]["dependencies"] = [{"service": "sqlite", "status": "ACTIVE"}]
        sweep(record)

        (finding,) = raised("connect-sync-stalled")

        self.assertIn({"label": "Sync", "value": "not reported"}, finding["evidence"])

    def test_a_run_that_never_reached_connect_says_nothing_of_sync(self):
        sweep(read(sync=None))

        self.assertEqual(raised("connect-sync-stalled"), [])


class UnreadTests(TestCase):
    def setUp(self):
        tailnet_connection()

    def test_a_missing_document_is_a_finding_naming_the_renderer(self):
        sweep(unread("missing"))

        (finding,) = raised("render-status-unread")

        self.assertEqual(finding["severity"], "attention")
        self.assertEqual(finding["title"], "HQ cannot tell whether example-controller's credentials are fresh")
        self.assertEqual(
            finding["evidence"],
            [{"label": "Job", "value": "hq"}, {"label": "Report", "value": "No report found"}],
        )
        self.assertEqual(
            [step["command"] for step in finding["operator_steps"]], ['ssh example-controller "sudo systemctl start severino-hq-secrets.service"']
        )
        for name in RULES:
            if name != "render-status-unread":
                self.assertEqual(raised(name), [])

    def test_every_reason_is_said_in_words(self):
        for reason in RENDER_REASONS:
            with self.subTest(reason=reason):
                sweep(unread(reason))

                (finding,) = raised("render-status-unread")

                self.assertEqual(finding["evidence"][1]["value"], rules.UNREAD_REASONS[reason])

    def test_one_unread_beside_one_healthy_names_only_the_unread(self):
        sweep(read(), unread("invalid", "other-apps"))

        (finding,) = raised("render-status-unread")

        self.assertEqual([item["value"] for item in finding["evidence"] if item["label"] == "Job"], ["other-apps"])

    def test_a_record_the_schema_refused_is_a_finding_not_a_silence(self):
        record_inventory(
            {RENDER_STATUS_KIND: {"ok": True, "records": [{"renderer": "hq", "state": "fine"}]}},
            principal=cli_principal(),
        )
        self.assertEqual(ProviderInventory.objects.get(kind=RENDER_STATUS_KIND).records, [])

        (finding,) = raised("render-status-unread")

        self.assertIn(
            {"label": "Report", "value": "Report could not be understood"}, finding["evidence"]
        )

    def test_a_reading_the_controller_could_not_take_is_a_finding(self):
        store(RENDER_STATUS_KIND, reachable=False, error="the render status list is not name=path pairs")

        (finding,) = raised("render-status-unread")

        self.assertIn(
            {"label": "Report", "value": "The controller could not read it"}, finding["evidence"]
        )


class SubjectTests(TestCase):
    def test_a_reading_whose_controller_no_node_stands_for_is_still_raised(self):
        sweep(unread("missing"))

        (finding,) = raised("render-status-unread")

        self.assertTrue(finding["subject"])
        self.assertIn("example-controller", finding["title"])

    def test_a_controller_that_is_a_machine_carries_it_on_the_machine(self):
        ManagedResource.objects.create(
            key="example-controller", kind="machine",
            spec={"name": "example-controller", "addresses": ["192.0.2.10"]},
        )
        tailnet_connection()
        sweep(unread("missing"))

        (finding,) = raised("render-status-unread")

        self.assertTrue(finding["subject"].startswith("machine:"), finding["subject"])
        self.assertIn("example-controller", finding["title"])

    def test_the_check_reads_the_reading_again(self):
        tailnet_connection()
        sweep(unread("missing"))

        (finding,) = raised("render-status-unread")

        self.assertEqual(finding["scope"], RENDER_STATUS_KIND)
        verify = [
            action for step in finding["workflow"]["steps"] for action in step["actions"] if action["name"] == "verify"
        ]
        self.assertEqual(len(verify), 1)
        self.assertIn(f"kind={RENDER_STATUS_KIND}", verify[0]["url"])

    def test_each_finding_reaches_the_action_queue(self):
        tailnet_connection()
        sweep(read(outcome="failed", failure="host", sync="TOKEN_NEEDED"), unread("missing", "other-apps"))

        with projection_scope():
            keys = {item["key"] for item in work_queue()}
            found = {
                finding.rule: finding.subject
                for finding in derive_findings(derive_topology(principal=EVERYTHING), principal=EVERYTHING)
                if finding.rule in RULES
            }

        self.assertEqual(set(found), {"render-failing", "connect-sync-stalled", "render-status-unread"})
        for rule, subject in found.items():
            with self.subTest(rule=rule):
                self.assertIn(f"hq.infrastructure:finding:{rule}:{subject}", keys)


class PageTests(TestCase):
    """The findings as a person meets them, with every link they carry followed."""

    def setUp(self):
        tailnet_connection()
        sweep(read(outcome="failed", failure="host", sync="TOKEN_NEEDED"), unread("missing", "other-apps"))
        self.user = an_operator()
        self.client.force_login(self.user)

    def test_the_pages_show_the_command_and_every_remedy_link_opens_ready(self):
        emitted = []
        for page in (reverse("control_plane:findings"), reverse("action_items")):
            response = self.client.get(page)
            self.assertEqual(response.status_code, 200)
            body = response.content.decode()
            self.assertIn("could not refresh its credentials from 1Password", body)
            self.assertIn("sudo systemctl start severino-hq-secrets.service", body)
            emitted += [
                (unescape(match.group(1)), "GET")
                for match in re.finditer(r'<a [^>]*href="(/commands/[^"]+)"', body)
            ]
        principal = web_principal(self.user)
        with projection_scope():
            for finding in derive_findings(derive_topology(principal=principal), principal=principal):
                if finding.rule not in RULES:
                    continue
                emitted += [(remedy.url, remedy.method) for remedy in finding.remedies]
                emitted += [
                    (action.url, action.method) for step in finding.workflow.steps for action in step.actions
                ]
        self.assertTrue(emitted)
        for label_url in _command_links([("", url, method) for url, method in emitted]):
            with self.subTest(url=label_url[1]):
                response = self.client.get(label_url[1])
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, UNOFFERED)
                self.assertNotContains(response, TRAP)


class FactTests(SimpleTestCase):
    def test_a_fact_carries_a_rendering_whole(self):
        rendering = rules.Rendering(
            "hq", "read", "", "failed", "connect_denied", "2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z",
            "2025-12-31T23:00:00Z", "TOKEN_NEEDED", "2026-01-02T00:00:00Z",
        )

        key, value = rendering.fact

        self.assertEqual(key, rules.RENDER_STATUS)
        self.assertEqual(rules.Rendering.of(value), rendering)

    def test_the_threshold_is_asked_of_the_estates_now(self):
        at = datetime(2026, 1, 1, tzinfo=utc.utc)
        rendering = rules.Rendering("hq", "read", confirmed_at=at.isoformat(), rendered_at=at.isoformat())

        self.assertFalse(rendering.unconfirmed(at + rules.CONFIRMED_WITHIN))
        self.assertTrue(rendering.unconfirmed(at + rules.CONFIRMED_WITHIN + timedelta(seconds=1)))
        self.assertFalse(rendering.unrendered(at + rules.RENDERED_WITHIN))
        self.assertTrue(rendering.unrendered(at + rules.RENDERED_WITHIN + timedelta(seconds=1)))
        self.assertTrue(rules.Rendering("hq", "read").unconfirmed(at))
