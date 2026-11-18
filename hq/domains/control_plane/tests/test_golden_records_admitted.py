"""Every reading's contract, walked through its examples.

The golden file holds every reader's record types as the controller writes
them (``TestRecordsGoldenHoldsEveryReadersRecords``): each is an admitted
example of its kind, so a record HQ would refuse there is a shape the two
sides disagree on, which a sweep would drop. ``record_examples`` holds the
rest: what each kind refuses, and what it keeps of a value it rewrites.
"""

import copy
import json
import pkgutil
from importlib import import_module
from pathlib import Path
from typing import Any

from django.conf import settings
from django.test import SimpleTestCase, tag
from pydantic import ValidationError

from hq.domains.control_plane.observations import OBSERVATIONS
from hq.platform.application.json_schemas import json_schema

from . import record_examples
from .record_examples import ABSENT, AS_GIVEN, Kept, Refused

GOLDEN = Path(settings.BASE_DIR) / "controller/providers/testdata/golden/records.json"
# A member no schema names, planted in every object a schema names members of.
UNNAMED = "unnamed_example"


def _golden() -> dict[str, list[dict[str, Any]]]:
    # A resource kind's inventory is stored as reported: no schema admits it.
    written = json.loads(GOLDEN.read_text(encoding="utf-8"))
    return {kind: records for kind, records in written.items() if kind in OBSERVATIONS}


def _examples() -> dict[str, tuple[Refused | Kept, ...]]:
    found: dict[str, tuple[Refused | Kept, ...]] = {}
    for module in pkgutil.iter_modules(record_examples.__path__):
        for kind, examples in import_module(f"{record_examples.__name__}.{module.name}").EXAMPLES.items():
            found[kind] = found.get(kind, ()) + examples
    return found


def _steps(field: str) -> list[str | int]:
    return [int(step) if step.isdigit() else step for step in field.split(".")]


def _with(record: dict[str, Any], field: str, value: Any) -> dict[str, Any]:
    changed = copy.deepcopy(record)
    *parents, last = _steps(field)
    node: Any = changed
    for step in parents:
        node = node[step]
    if value is ABSENT:
        del node[last]
    else:
        node[last] = value
    return changed


def _at(record: Any, field: str) -> Any:
    for step in _steps(field):
        record = record[step]
    return record


def _object(schema: dict[str, Any], definitions: dict[str, Any]) -> dict[str, Any]:
    """The schema itself, past a reference and the null a nullable member also takes."""

    while True:
        if "$ref" in schema:
            schema = definitions[schema["$ref"].rsplit("/", 1)[1]]
        elif "anyOf" in schema and len(taken := [each for each in schema["anyOf"] if each.get("type") != "null"]) == 1:
            schema = taken[0]
        else:
            return schema


def _planted(value: Any, schema: dict[str, Any], definitions: dict[str, Any]) -> Any:
    schema = _object(schema, definitions)
    if isinstance(value, dict) and "properties" in schema:
        members = schema["properties"]
        planted = {name: _planted(each, members.get(name, {}), definitions) for name, each in value.items()}
        return {**planted, UNNAMED: "example"}
    if isinstance(value, list):
        return [_planted(each, schema.get("items", {}), definitions) for each in value]
    return value


@tag("source")
class GoldenRecordsAdmittedTests(SimpleTestCase):
    def test_every_record_the_controller_writes_is_admitted_by_its_kind(self):
        refused = []
        for kind, records in _golden().items():
            for index, record in enumerate(records):
                try:
                    OBSERVATIONS[kind].record.model_validate(record)
                except ValidationError as error:
                    where = ", ".join(".".join(map(str, found["loc"])) for found in error.errors())
                    refused.append(f"{kind} record {index}: {where}")
        self.assertEqual(
            refused,
            [],
            "HQ's schema for each kind refuses these records the controller writes, so a sweep drops them. "
            "Make the controller's record type and the kind's record (hq/domains/control_plane/observations/) "
            "agree, then rewrite the golden file:\n" + "\n".join(refused),
        )

    def test_a_member_the_schema_does_not_name_is_dropped_at_every_depth(self):
        for kind, records in _golden().items():
            schema = json_schema(OBSERVATIONS[kind].record)
            for record in records:
                with self.subTest(kind=kind):
                    kept, refused = OBSERVATIONS[kind].clean([_planted(record, schema, schema.get("$defs", {}))])
                    self.assertEqual(refused, 0)
                    self.assertNotIn(UNNAMED, json.dumps(kept))

    def test_each_example_is_refused_by_its_field_or_kept_as_it_says(self):
        golden = _golden()
        for kind, examples in _examples().items():
            spec = OBSERVATIONS[kind]
            for example in examples:
                record = _with(golden[kind][0], example.field, example.value)
                with self.subTest(kind=kind, field=example.field, value=example.value):
                    if isinstance(example, Kept):
                        (kept,), _ = spec.clean([record])
                        expected = example.value if example.kept is AS_GIVEN else example.kept
                        self.assertEqual(_at(kept, example.field), expected)
                        continue
                    with self.assertRaises(ValidationError) as refusal:
                        spec.record.model_validate(record)
                    where = {".".join(map(str, found["loc"])) for found in refusal.exception.errors()}
                    self.assertEqual({each for each in where if not f"{each}.".startswith(f"{example.field}.")}, set())
