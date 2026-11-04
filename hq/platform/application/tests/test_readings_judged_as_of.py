"""A reading of a machine is judged as of when it was taken, and once it is too
old to judge it is said to be unread, with when it was read."""

import json
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.domains.control_plane.observations.host import RENDER_STATUS_KIND, UNIT_KIND
from hq.domains.control_plane.tests import bridge_client

from ..cadence import slowest_sweep_interval
from ..derivation_testing import frozen, uncached
from ..findings import RULES, derive_findings
from ..moments import when
from ..reading_facts import FactRow
from ..render_status_findings import Rendering
from ..topology import derive_topology
from ..unit_findings import UnitState
from . import test_render_status as render, test_unit_findings as units
from .test_tailnet_posture import EVERYTHING, raised, tailnet_connection

# Past the slowest the controller ever reads, so nothing it says is current.
AGED = slowest_sweep_interval() + timedelta(minutes=10)
# The production case: read twelve hours ago, the renderer current when read.
READ_AGO = timedelta(hours=12, minutes=10)
JUST_BEFORE = timedelta(minutes=50)


def unread_evidence(rule: str) -> dict[str, str]:
    (finding,) = raised(rule)
    return {item["label"]: item["value"] for item in finding["evidence"]}


def refuse_a_sweep() -> None:
    """A sweep the bridge refuses whole, its readings with it."""

    body = {RENDER_STATUS_KIND: {"ok": "true", "records": []}}
    answer = bridge_client.request(
        "/inventory", query={"controller-id": "example-controller"}, body=json.dumps(body).encode()
    )
    assert answer.status == 422, answer.body


class ProductionCaseTests(TestCase):
    """A render status read twelve hours ago, the renderer refreshed shortly before."""

    def setUp(self):
        tailnet_connection()
        before = READ_AGO + JUST_BEFORE
        render.sweep(render.read(attempted=before, confirmed=before, rendered=before), age=READ_AGO)
        self.read_at = ProviderInventory.objects.get(kind=RENDER_STATUS_KIND).observed_at

    def test_it_is_unread_since_its_reading_and_never_stale_against_now(self):
        self.assertEqual(raised("render-stale"), [])
        self.assertEqual(unread_evidence("render-status-unread").get("Last read"), when(self.read_at))

    def test_an_open_refusal_is_named_as_its_cause(self):
        refuse_a_sweep()

        said = unread_evidence("render-status-unread")

        self.assertEqual(said.get("Last read"), when(self.read_at))
        self.assertIn(f"/{RENDER_STATUS_KIND}/ok", said.get("Why it has not been read since", ""))


# Each reading of a machine, as a reading that raises every rule over it when
# fresh: its stamps a moment, a day or hours before it was taken.
def _aged_render_status() -> None:
    record = render.read(
        outcome="failed",
        failure="connect_denied",
        attempted=AGED + timedelta(minutes=5),
        confirmed=AGED + timedelta(days=1),
        rendered=AGED + timedelta(days=2),
        sync="TOKEN_NEEDED",
    )
    render.sweep(record, age=AGED)


def _aged_units() -> None:
    def at(before: timedelta) -> str:
        return units.stamp(AGED + before)

    units.sweep(
        units.failed(
            started_at=at(timedelta(minutes=6)),
            ended_at=at(timedelta(minutes=5)),
            condition_at=at(timedelta(minutes=6)),
            read_at=at(timedelta()),
        ),
        units.timer(
            last_trigger_at=at(timedelta(days=1)), next_elapse_at=at(timedelta(hours=3)), read_at=at(timedelta())
        ),
        age=AGED,
    )


READINGS = {Rendering: (RENDER_STATUS_KIND, _aged_render_status), UnitState: (UNIT_KIND, _aged_units)}
KINDS = frozenset(kind for kind, _store in READINGS.values())


def said(now) -> dict[tuple[str, str, str], tuple]:
    """What every rule says about the machine readings at ``now``, by rule, subject and kind."""

    with frozen(now), uncached():
        raised = derive_findings(derive_topology(principal=EVERYTHING), principal=EVERYTHING)
    return {
        (finding.rule, finding.subject, finding.scope): (finding.title, finding.explanation, finding.evidence)
        for finding in raised
        if finding.scope in KINDS
    }


class AgedReadingGuardTests(TestCase):
    """Every rule in ``findings.RULES``, over every reading of a machine aged
    past the slowest sweep."""

    def setUp(self):
        tailnet_connection()
        for _kind, store in READINGS.values():
            store()
        self.taken = {row.kind: row.observed_at for row in ProviderInventory.objects.filter(kind__in=KINDS)}

    def test_every_reading_of_a_machine_is_covered(self):
        self.assertEqual(set(READINGS), set(FactRow.__subclasses__()))
        self.assertTrue(RULES)

    def test_the_readings_raise_their_rules_when_judged_as_they_were_taken(self):
        fired = {rule for rule, _subject, _kind in said(min(self.taken.values()) + timedelta(minutes=1))}

        self.assertLessEqual({"render-failing", "connect-sync-stalled", "unit-failed", "timer-stalled"}, fired)

    def test_no_rule_measures_an_aged_reading_from_now(self):
        now = timezone.now()
        aged, later = said(now), said(now + timedelta(days=1))

        self.assertEqual(aged, later)
        self.assertEqual({rule for rule, _subject, _kind in aged}, {"render-status-unread", "unit-state-unread"})
        for (rule, _subject, kind), (_title, _explanation, evidence) in aged.items():
            with self.subTest(rule=rule):
                self.assertIn(("Last read", when(self.taken[kind])), evidence)
