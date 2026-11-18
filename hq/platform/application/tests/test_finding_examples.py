"""Every finding rule walked through its examples (``finding_examples``)."""

from contextlib import AbstractContextManager, nullcontext
from importlib import import_module
from importlib.util import find_spec
from itertools import zip_longest
from typing import Any
from unittest import mock

from django.db import transaction
from django.test import TestCase, override_settings

from hq.domains.control_plane.models import ManagedResource
from hq.domains.control_plane.observations import OBSERVATIONS

from .. import hq_self
from ..action_items import item_key
from ..attention import finding_why
from ..dashboard import work_queue
from ..derivation_testing import frozen, uncached
from ..finding_model import Finding, FindingRule
from ..finding_steps import Press
from ..findings import RULE_MODULES, _estate, estate_findings, finding_key, read_gate
from ..infrastructure import ResourceState, resource_health
from ..inventory_testing import store
from ..projection import projection_scope
from ..security import Capability, Principal
from ..tone import Tone
from ..topology import derive_topology
from .finding_examples import AT, Ago, Example, Examples, Raised, Stored, connection

EVERYTHING = Principal("test", "operator", frozenset(Capability))
# Where the queue keeps a finding of a rule about the estate rather than a section's records.
ESTATE = "hq.infrastructure"
LOOPBACK = ("127.0.0.1", "::1")
# The rule modules whose tests still hold their rules, each to move to
# examples of its own; the list only shrinks.
UNWALKED = frozenset(
    {
        "controller_findings",
    }
)


def _short(module: Any) -> str:
    return module.__name__.rsplit(".", 1)[1]


def examples_of(module: Any) -> tuple[Examples, ...]:
    if _short(module) in UNWALKED:
        return ()
    return import_module(f"hq.platform.application.tests.finding_examples.{_short(module)}").EXAMPLES


def _written(value: Any) -> Any:
    if isinstance(value, Ago):
        return value.written()
    if isinstance(value, dict):
        return {key: _written(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_written(item) for item in value]
    return value


def _store(readings, *, failed: bool = False) -> None:
    for kind, reading in readings.items():
        records, fields = (reading.records, dict(reading.fields)) if isinstance(reading, Stored) else (reading, {})
        records = [_written(record) for record in records]
        if kind in OBSERVATIONS:
            kept, refused = OBSERVATIONS[kind].clean(records)
            assert not refused, f"the schema of {kind} refuses an example's record"
            records = kept
        if failed:
            records, fields = [], {**fields, "reachable": False, "error": "unreachable"}
        store(kind, *records, **fields)


def a_controller() -> None:
    """The controller every example's estate is read by, on its own machine."""

    connection("example-tailnet", "tailscale")


class Walked:
    """What one example's estate raises, read as every door reads it."""

    def __init__(self, rule: FindingRule, example: Example, *, failed: bool = False):
        a_controller()
        _store(example.readings, failed=failed)
        with (
            _entered(example.given() if example.given else None),
            override_settings(**example.settings),
            mock.patch.object(hq_self, "host_addresses", return_value=frozenset({*LOOPBACK, *example.hq_at})),
            uncached(),
            projection_scope(),
        ):
            topology = derive_topology(principal=EVERYTHING)
            self.detected = rule.detect(_estate(topology, EVERYTHING))
            self.found: tuple[Finding, ...] = estate_findings(principal=EVERYTHING, rule=rule.name)
            self.queued = {item["key"] for item in work_queue()}
        self.subjects = {node.id: node for node in topology.nodes}


def _entered(held: object) -> AbstractContextManager[object]:
    """What ``given`` returned, where it is what the estate is read inside."""

    return held if isinstance(held, AbstractContextManager) else nullcontext()


def said(finding: Finding, raised: Raised | None = None) -> tuple[Any, ...]:
    """What ``finding`` says, in the terms ``raised`` states it in: its
    evidence and steps only where ``raised`` names them."""

    told = tuple(step for step in finding.steps if not isinstance(step, Press))
    presses = tuple(press.capability for press in finding.presses)
    return (
        finding.title,
        finding.severity,
        presses,
        finding.evidence if raised is None or raised.evidence is not None else None,
        told if raised is None or raised.steps is not None else None,
    )


def stated(rule: FindingRule, raised: Raised) -> tuple[Any, ...]:
    """What an example says a finding says, its rule's severity where it leaves it out."""

    return (raised.title, raised.severity or rule.severity, raised.presses, raised.evidence, raised.steps)


class EveryRuleTests(TestCase):
    """Each rule raises what its examples say and nothing where they say
    nothing, and each finding it raises keeps every finding's promises: in
    title order, at a severity its rule declares a bar for, with a step to
    take and something to open or press, a read gate derived from what it is
    about, a row in the queue, and nothing raised of a reading that failed
    that the reading would not raise, as if a failure read as nothing there."""

    def setUp(self):
        import time_machine  # a dev dependency, which the image does not install

        self.enterContext(time_machine.travel(AT, tick=False))
        self.enterContext(frozen(AT))

    def test_every_rule_module_declares_examples_of_each_of_its_rules(self):
        for module in RULE_MODULES:
            with self.subTest(module=module.__name__):
                if _short(module) in UNWALKED:
                    self.assertIsNone(
                        find_spec(f"hq.platform.application.tests.finding_examples.{_short(module)}"),
                        "a module with examples leaves UNWALKED",
                    )
                    continue
                self.assertEqual(
                    [examples.rule for examples in examples_of(module)], [rule.name for rule in module.RULES]
                )

    def test_every_example_of_every_rule(self):
        for module in RULE_MODULES:
            rules = {rule.name: rule for rule in module.RULES}
            for examples in examples_of(module):
                rule = rules[examples.rule]
                # What it raises of a reading that failed, where that is what it is about.
                of_failure = {
                    raised.title for example in examples.fires if _fails(example) for raised in example.raises
                }
                for index, example in enumerate((*examples.fires, *examples.quiet)):
                    with self.subTest(rule=rule.name, example=index), transaction.atomic():
                        self._walk(rule, example)
                        transaction.set_rollback(True)
                    if example.readings and not _fails(example):
                        with self.subTest(rule=rule.name, example=index, readings="failed"), transaction.atomic():
                            walked = Walked(rule, example, failed=True)
                            transaction.set_rollback(True)
                            self.assertLessEqual(
                                {finding.title for finding in walked.found},
                                of_failure | {raised.title for raised in example.raises},
                                "a reading that failed raises nothing it would not raise read, but what it "
                                "raises of the failure",
                            )

    def _walk(self, rule: FindingRule, example: Example) -> None:
        walked = Walked(rule, example)
        titles = [finding.title for finding in walked.detected]
        self.assertEqual(titles, sorted(titles), "its detector raises in title order")
        found = sorted(walked.found, key=lambda finding: finding.title)
        self.assertEqual(
            [said(finding, raised) if finding else None for finding, raised in zip_longest(found, example.raises)],
            [stated(rule, raised) for raised in example.raises],
            "it raised, as an example states it:\n" + "\n".join(_as_example(rule, finding) for finding in found),
        )
        for finding in found:
            self.assertIn(finding.severity, rule.bars)
            self.assertIn(rule.bars[finding.severity], finding_why(finding, rule))
            self.assertTrue(finding.steps, "it leaves a step to take")
            self.assertTrue(
                finding.presses or finding.subject or finding.primary, "it has something to press or a page to open"
            )
            gate = read_gate(finding, walked.subjects.get(finding.subject))
            self.assertIsNotNone(gate, "its read gate is derived from what it is about")
            self.assertEqual(finding.read_capabilities, gate)
            if finding.scope:
                self.assertIn(
                    f"kind={finding.scope}",
                    finding.check.url if finding.check else "",
                    "a claim about a kind offers to read it again",
                )
            if finding.severity.acted_on and not finding.awaiting_fix and _confirmed(finding):
                self.assertIn(item_key(rule.domain or ESTATE, finding_key(finding)), walked.queued)


def _confirmed(finding: Finding) -> bool:
    """Whether the queue speaks for its subject yet: a declared resource
    waits until HQ has confirmed it."""

    if not finding.subject.startswith("resource:"):
        return True
    resource = ManagedResource.objects.get(key=finding.subject.removeprefix("resource:"))
    return resource_health(resource)["state"] not in {ResourceState.PENDING, ResourceState.DECLARED}


def _fails(example: Example) -> bool:
    """Whether the example is itself of a reading that failed."""

    return any(
        isinstance(reading, Stored) and (reading.fields.get("reachable") is False or reading.fields.get("error"))
        for reading in example.readings.values()
    )


def _as_example(rule: FindingRule, finding: Finding) -> str:
    """``finding`` written as the ``Raised`` an example states, leaving out what its rule says."""

    parts = [f"title={finding.title!r}"]
    if finding.severity != rule.severity:
        parts.append(f"severity=Tone.{Tone(finding.severity).name}")
    if presses := tuple(press.capability for press in finding.presses):
        parts.append(f"presses={presses!r}")
    return f"Raised({', '.join(parts)})," if len(parts) > 1 else f"{finding.title!r},"
