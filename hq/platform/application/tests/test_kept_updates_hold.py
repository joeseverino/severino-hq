"""Every update that keeps what it is not sent reads it back whole, and keeps
it as the record is locked: a field changed on the row after the caller's
read, and not sent, survives the write. Held over every record the showcase
estate holds for each such update, so a new one is held from its first record."""

import base64
from typing import Any
from unittest import mock

from django.db import transaction
from django.test import TestCase

from hq.domains.control_plane.models import ProviderConnection
from hq.platform.core import seeds
from hq.platform.core.example_estate.testing import throwaway_media

from .. import capabilities
from ..approvals import AWAITING_APPROVAL
from ..capabilities import capability_registry, execute_capability
from ..integration_specs import Kept, command_schema, kept_by
from ..resources import list_resource, resource_registry
from ..security import cli_principal

# What a write that always sends a field (``Kept.always_sent``) sends there,
# by field name: a field with no value here fails the guard, so a new one adds one.
ALWAYS_SENT: dict[str, Any] = {
    "file": {
        "name": "example-receipt.pdf",
        "content_type": "application/pdf",
        "content": base64.b64encode(b"%PDF-1.4\n%example receipt\n").decode(),
    },
}


def _kept_specs():
    return [(spec, kept) for spec in capability_registry().values() if (kept := kept_by(spec)) is not None]


def _targets(spec) -> list[Any]:
    """The target of every record the showcase holds that ``spec`` writes to,
    as a caller sends it (a number, or text): each row of the model its
    resource is searched in, or, for records no model of HQ's holds, each
    item its resource lists."""

    resource = resource_registry()[spec.subject_resource]
    if resource.search is not None:
        rows = resource.search.model._default_manager.values_list(resource.search.identifier_field, flat=True)
        return [row if isinstance(row, int) else str(row) for row in rows]
    listed = list_resource(spec.subject_resource, {}, principal=cli_principal(), fields=["*"])
    return [item[resource.identifier] for item in listed["items"]]


def _made(spec, payload: dict[str, Any], target: Any) -> bool:
    """Whether HQ made the write ``spec`` is asked for on ``target``, rather
    than refusing it or holding it for a person to agree to."""

    answer = execute_capability(spec.name, payload, principal=cli_principal(), target=target)
    return bool(answer.get("ok")) and answer.get("status") != AWAITING_APPROVAL


def _others(value: Any, schema: dict[str, Any], held: list[Any]) -> list[Any]:
    """Values a field could be changed to from ``value``: what another record
    holds there, another of its choices, the other truth value, or the number
    or text beside it. One HQ refuses is passed over."""

    choices = [choice for option in (schema, *schema.get("anyOf", ())) for choice in option.get("enum", ())]
    beside = []
    if isinstance(value, bool):
        beside = [not value]
    elif isinstance(value, int):
        beside = [value + 1]
    elif isinstance(value, str):
        beside = [f"{value}-edited" if value else "edited"]
    others = []
    for other in (*held, *choices, *beside):
        if other != value and other not in others:
            others.append(other)
    return others


class AKeptUpdateKeepsTheLockedRowTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.enterClassContext(throwaway_media())
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        cls.enterClassContext(seeds.built("showcase"))
        # HQ may act through every connection, so no change is refused for
        # one that only reads and the update is asked of every kind.
        ProviderConnection.objects.update(manages=True)

    def test_every_kept_update_is_held_over_a_record_the_showcase_holds(self):
        specs = _kept_specs()
        self.assertIn("infrastructure.resource.update", {spec.name for spec, _ in specs})
        for spec, _kept in specs:
            with self.subTest(spec.name):
                self.assertTrue(_targets(spec), f"the showcase holds no record {spec.name} writes to")

    def test_a_kept_read_answers_every_field_of_the_command_but_those_always_sent(self):
        for spec, kept in _kept_specs():
            fields = set(command_schema(spec.command_type)["properties"]) - kept.always_sent
            for target in _targets(spec):
                with self.subTest(spec.name, target=target):
                    self.assertEqual(set(kept.read(target)), fields)

    def test_a_field_changed_after_the_read_and_not_sent_survives_the_update(self):
        for spec, kept in _kept_specs():
            with self.subTest(spec.name):
                missing = kept.always_sent - set(ALWAYS_SENT)
                self.assertFalse(missing, f"ALWAYS_SENT gives {spec.name} nothing to send as {missing}")
                held = self._held_edits(spec, kept)
                self.assertTrue(held, f"no field of {spec.name} could be changed on a showcase record")

    def _held_edits(self, spec, kept: Kept) -> list[str]:
        """Each field that could be changed on a record between a read and an
        update that sent nothing of it, once the update left the change standing.
        A record's own name is not changed: it moves the target, which the
        update then names no longer."""

        sent = {name: ALWAYS_SENT[name] for name in kept.always_sent}
        properties = command_schema(spec.command_type)["properties"]
        reads = {target: dict(kept.read(target)) for target in _targets(spec)}
        return [
            name
            for name in set(properties) - kept.always_sent
            if any(
                self._held_edit(spec, kept, target, stale, name, value, sent)
                for target, stale in reads.items()
                if stale[name] != target
                for value in _others(stale[name], properties[name], [read[name] for read in reads.values()])
            )
        ]

    def _held_edit(self, spec, kept, target, stale, name, value, sent) -> bool:
        """Change ``name`` to ``value`` on ``target`` after ``stale`` was read,
        update it sending none of it, and roll both back. False when HQ
        refuses the change itself, so there is nothing to keep."""

        sid = transaction.savepoint_create()
        try:
            if not _made(spec, {**sent, name: value}, target):
                return False
            changed = kept.read(target)[name]
            if changed == stale[name]:
                return False
            stale_read = Kept(lambda _target: stale, kept.always_sent)
            with mock.patch.object(
                capabilities, "kept_by", lambda asked: stale_read if asked.name == spec.name else None
            ):
                self.assertTrue(_made(spec, dict(sent), target), f"{spec.name} refused an update that sent nothing")
            self.assertEqual(kept.read(target)[name], changed, f"{spec.name} wrote {name} back as it was read")
            return True
        finally:
            transaction.savepoint_rollback(sid)
