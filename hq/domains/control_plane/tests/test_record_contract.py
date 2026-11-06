"""A kind's fingerprint moves exactly when what a stored record of it may hold does."""

from django.test import SimpleTestCase

from ..observations import ObservationRecord, ObservationSpec, ReadingPart


class Before(ObservationRecord):
    name: str
    api_token: str = ""


class After(ObservationRecord):
    name: str
    api_token: str = ""
    count: int = 0


def spec(record: type[ObservationRecord], *parts: ReadingPart) -> ObservationSpec:
    return ObservationSpec(kind="example.thing", provider="example", label="Thing", record=record, parts=parts)


class FingerprintTests(SimpleTestCase):
    def test_the_same_contract_has_the_same_fingerprint(self):
        self.assertEqual(spec(Before).fingerprint({"name"}), spec(Before).fingerprint({"name"}))

    def test_a_record_that_holds_another_shape_moves_it(self):
        self.assertNotEqual(spec(Before).fingerprint(()), spec(After).fingerprint(()))

    def test_a_member_the_door_now_keeps_moves_it(self):
        self.assertNotEqual(spec(Before).fingerprint(()), spec(Before).fingerprint({"api_token"}))

    def test_a_part_the_door_now_keeps_moves_it(self):
        # A part's name keys what a record counts by part, outside its schema.
        scanned = ReadingPart("leaked_secrets", "Leaked secrets")
        self.assertNotEqual(
            spec(Before, scanned).fingerprint(()), spec(Before, scanned).fingerprint({"leaked_secrets"})
        )

    def test_a_name_declared_elsewhere_does_not_move_it(self):
        self.assertEqual(spec(Before).fingerprint(()), spec(Before).fingerprint({"another_kinds_member"}))
