"""Every record the controller writes is one HQ's schema for its kind admits.

The golden file holds every reader's record types as the controller writes
them (``TestRecordsGoldenHoldsEveryReadersRecords``), so a record HQ would
refuse here is a shape the two sides disagree on, which a sweep would drop.
"""

import json
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, tag
from pydantic import ValidationError

from hq.domains.control_plane.observations import OBSERVATIONS

GOLDEN = Path(settings.BASE_DIR) / "controller/providers/testdata/golden/records.json"


@tag("source")
class GoldenRecordsAdmittedTests(SimpleTestCase):
    def test_every_record_the_controller_writes_is_admitted_by_its_kind(self):
        refused = []
        for kind, records in json.loads(GOLDEN.read_text(encoding="utf-8")).items():
            spec = OBSERVATIONS.get(kind)
            if spec is None:
                # A resource kind's inventory is stored as reported: no schema admits it.
                continue
            for index, record in enumerate(records):
                if spec.clean([record])[1]:
                    try:
                        spec.record.model_validate(record)
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
