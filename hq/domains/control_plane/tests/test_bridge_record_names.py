"""No member HQ's controller writes into a record is one the bridge's door strikes by its name.

The controller's tests write every reader's record types, every member set,
to ``controller/providers/testdata/golden/records.json`` and hold it current
(``TestRecordsGoldenHoldsEveryReadersRecords``), so this reads every member a
provider writes, not a sample.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from django.conf import settings
from django.test import SimpleTestCase, tag

from hq.domains.control_plane.bridge_application import declared_names
from hq.domains.control_plane.secret_shapes import struck

GOLDEN = Path(settings.BASE_DIR) / "controller/providers/testdata/golden/records.json"


def _golden() -> Iterator[tuple[str, dict[str, Any]]]:
    for kind, records in json.loads(GOLDEN.read_text(encoding="utf-8")).items():
        for record in records:
            yield kind, record


def _as_text(node: Any) -> Any:
    """The record with every value a string, the one value a name strikes."""

    if isinstance(node, dict):
        return {key: _as_text(child) for key, child in node.items()}
    if isinstance(node, list):
        return [_as_text(child) for child in node]
    return "example"


@tag("source")
class RecordNameTests(SimpleTestCase):
    def test_no_member_a_reader_writes_is_struck_by_its_name(self):
        declared = declared_names()
        found = sorted(
            {f"{kind} {pointer}" for kind, record in _golden() for pointer, _ in struck(_as_text(record), declared)[1]}
        )
        self.assertFalse(
            found,
            "The bridge's door strikes these members of what the controller reports, by their names, so HQ "
            "stores them as [redacted]. Declare each in its kind's record (hq/domains/control_plane/observations/), "
            "so the contract names it:\n" + "\n".join(found),
        )
