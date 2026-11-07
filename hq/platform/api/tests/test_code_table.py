"""Every code's facts are declared once, in ``refusal_status.CODES``: each set of
codes HQ answers or publishes is derived from it, and a capability is listed
only the codes its declaration lets it answer."""

from dataclasses import dataclass

from django.test import SimpleTestCase

from hq.platform.application import refusal_status
from hq.platform.application.refusal_status import (
    CODES,
    Code,
    Door,
    answered_by,
    capability_doors,
    statuses,
    waiting,
)

from .. import views

EXAMPLE = "example_added"


def _handler(command, *, principal):
    return {"ok": True}


def _checking_handler(command, *, principal, expected_updated_at=None):
    return {"ok": True}


@dataclass(frozen=True)
class _Spec:
    """What ``capability_doors`` reads of a capability: the facts it already declares."""

    effect: str = "write"
    target_kind: str | None = None
    handler: object = _handler


def _codes(spec, table=CODES):
    """The codes a call of ``spec`` can be answered with; for None, a call by any name."""

    return answered_by(*capability_doors(spec), table=table)


class DerivedSetTests(SimpleTestCase):
    def test_a_code_added_to_the_table_reaches_each_set_derived_from_it(self):
        table = {**CODES, EXAMPLE: Code(429, frozenset(Door), "it is an example", waits=True)}

        self.assertEqual(statuses(table)[EXAMPLE], 429)
        self.assertIn(EXAMPLE, waiting(table))
        for door in Door:
            with self.subTest(door=door):
                self.assertIn(EXAMPLE, answered_by(door, table=table))
        self.assertIn(EXAMPLE, _codes(None, table))
        self.assertIn(EXAMPLE, _codes(_Spec(effect="read"), table))

    def test_each_published_set_is_the_table_s(self):
        self.assertEqual(refusal_status.REFUSAL_STATUS, statuses())
        self.assertEqual(refusal_status.LATER_CODES, waiting())
        self.assertEqual(views.ENDPOINT_CODES, answered_by(Door.ADMISSION, Door.ENDPOINT))
        self.assertEqual(views.RESOURCE_CODES, answered_by(Door.RESOURCE))
        self.assertEqual(views.execute.__hq_codes__, views.ENDPOINT_CODES | answered_by(Door.BODY) | _codes(None))

    def test_every_code_has_a_door(self):
        for code, facts in CODES.items():
            with self.subTest(code=code):
                self.assertTrue(facts.doors)


class CapabilityCodeTests(SimpleTestCase):
    def test_a_read_is_never_answered_for_a_retry_key_or_a_hold(self):
        changes = answered_by(Door.RETRY_KEY, Door.HOLD, Door.DECISION)

        self.assertFalse(_codes(_Spec(effect="read")) & changes)
        self.assertLessEqual(changes, _codes(_Spec(effect="write")))

    def test_a_target_is_required_only_of_a_capability_that_acts_on_one(self):
        targeted = _codes(_Spec(target_kind="integer"))
        untargeted = _codes(_Spec())

        self.assertIn("target_required", targeted)
        self.assertNotIn("target_not_allowed", targeted)
        self.assertIn("target_not_allowed", untargeted)
        self.assertNotIn("target_required", untargeted)

    def test_a_stale_write_is_answered_only_by_a_handler_that_checks_for_one(self):
        table = {**CODES, EXAMPLE: Code(409, frozenset({Door.STALE_WRITE}), "it is an example")}

        self.assertIn(EXAMPLE, _codes(_Spec(handler=_checking_handler), table))
        self.assertNotIn(EXAMPLE, _codes(_Spec(), table))

    def test_only_a_call_by_name_can_name_an_unknown_capability(self):
        self.assertIn("unknown_capability", _codes(None))
        self.assertNotIn("unknown_capability", _codes(_Spec()))

    def test_what_a_handler_raises_on_its_own_reaches_every_capability(self):
        for spec in (_Spec(effect="read"), _Spec(target_kind="integer"), _Spec(handler=_checking_handler)):
            with self.subTest(spec=spec):
                self.assertLessEqual(answered_by(Door.CAPABILITY), _codes(spec))

    def test_a_call_by_name_lists_every_code_any_capability_does(self):
        everyone = _codes(None)
        for spec in (_Spec(effect="read"), _Spec(target_kind="integer"), _Spec(handler=_checking_handler)):
            with self.subTest(spec=spec):
                self.assertLessEqual(_codes(spec), everyone)
