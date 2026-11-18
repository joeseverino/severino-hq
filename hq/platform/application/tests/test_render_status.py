"""The secret render reading, and what HQ says when the secrets go stale."""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from django.test import SimpleTestCase, TestCase, tag
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.observations.host import (
    RENDER_REASONS,
    RENDER_STATUS_KIND,
    RENDERER_NAME,
    STATUS_WORD,
)
from hq.platform.application.markup_testing import all_elements

from .. import render_status_findings as rules
from ..findings import estate_findings
from ..inventory_testing import store
from ..security import web_principal
from .commands import commands_on
from .test_remedy_links import TRAP, UNOFFERED, _command_links, an_operator
from .test_tailnet_posture import raised, tailnet_connection

ROOT = Path(__file__).resolve().parents[4]
SPEC = OBSERVATIONS[RENDER_STATUS_KIND]
RULES = tuple(rule.name for rule in rules.RULES)


def stamp(age: timedelta) -> str:
    """An instant ``age`` ago, as the renderer writes one."""

    return (timezone.now() - age).astimezone(UTC).isoformat().replace("+00:00", "Z")


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


@tag("source")
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
        self.assertIn("RandomizedDelaySec=30s\n", timer)
        # gofmt aligns the field to its neighbors, so any run of spaces.
        self.assertRegex(main, r"FullEvery: +24 \* time\.Hour,")
        # Read from the timer, not restated beside it.
        self.assertEqual(rules.RENDER_EVERY, timedelta(hours=1))
        self.assertEqual(rules.RENDER_DELAY, timedelta(seconds=30))
        self.assertEqual(rules.FULL_READ_EVERY, timedelta(hours=24))
        self.assertEqual(rules.CONFIRMED_WITHIN, timedelta(hours=3, seconds=90))
        self.assertEqual(rules.RENDERED_WITHIN, timedelta(hours=27, seconds=90))

    def test_every_renderer_the_launcher_names_has_its_unit(self):
        layout = self.source("controller", "host", "layout.go")
        block = layout[layout.index("var Renderers = ") :]
        named = re.findall(r'^\t\{"([^"]+)", "[^"]+"\},$', block[: block.index("\n}\n")], re.MULTILINE)

        self.assertEqual(named, list(rules.RENDERER_UNITS))
        for name, unit in rules.RENDERER_UNITS.items():
            with self.subTest(renderer=name):
                self.assertRegex(name, RENDERER_NAME)
                self.assertTrue((ROOT / "deploy" / "systemd" / unit).is_file())
                self.assertTrue((ROOT / "deploy" / "systemd" / unit.replace(".service", ".timer")).is_file())


class SubjectTests(TestCase):
    def test_a_controller_that_is_a_machine_carries_it_on_the_machine(self):
        ManagedResource.objects.create(
            key="example-controller",
            kind="machine",
            spec={"name": "example-controller", "addresses": ["192.0.2.10"]},
        )
        tailnet_connection()
        sweep(unread("missing"))

        (finding,) = raised("render-status-unread")

        self.assertTrue(finding["subject"].startswith("machine:"), finding["subject"])
        self.assertIn("example-controller", finding["title"])


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
            self.assertIn("sudo systemctl start severino-hq-secrets.service", commands_on(body))
            emitted += [
                (element.attrs["href"], "GET")
                for element in all_elements(body)
                if element.tag == "a" and element.attrs.get("href", "").startswith("/commands/")
            ]
        principal = web_principal(self.user)
        for finding in estate_findings(principal=principal):
            if finding.rule not in RULES:
                continue
            offered = (*finding.presses, *((finding.check,) if finding.check else ()))
            emitted += [(action.url, action.method) for action in offered]
        self.assertTrue(emitted)
        for label_url in _command_links([("", url, method) for url, method in emitted]):
            with self.subTest(url=label_url[1]):
                response = self.client.get(label_url[1])
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, UNOFFERED)
                self.assertNotContains(response, TRAP)


class ThresholdTests(SimpleTestCase):
    def test_the_threshold_is_judged_as_of_the_reading(self):
        at = datetime(2026, 1, 1, tzinfo=UTC)

        def read_after(age: timedelta) -> rules.Rendering:
            return rules.Rendering(
                renderer="hq",
                state="read",
                confirmed_at=at.isoformat(),
                rendered_at=at.isoformat(),
                read_at=(at + age).isoformat(),
            )

        second = timedelta(seconds=1)
        self.assertFalse(read_after(rules.CONFIRMED_WITHIN).unconfirmed)
        self.assertTrue(read_after(rules.CONFIRMED_WITHIN + second).unconfirmed)
        self.assertFalse(read_after(rules.RENDERED_WITHIN).unrendered)
        self.assertTrue(read_after(rules.RENDERED_WITHIN + second).unrendered)
        self.assertTrue(rules.Rendering(renderer="hq", state="read").unconfirmed)
