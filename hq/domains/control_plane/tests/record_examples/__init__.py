"""What each reading's schema refuses, and what it keeps of a value it rewrites.

A kind's admitted examples are its records in the controller's golden file
(``controller/providers/testdata/golden/records.json``). An observations
module's other examples live in the module of the same name here, as
``EXAMPLES``: each a change to the kind's first golden record, at a dotted
path. One test walks them all (``test_golden_records_admitted``), so a new
kind needs its examples and no test. Kept with the tests, so no image ships
them.
"""

from dataclasses import dataclass
from typing import Any

# A value that leaves the field out of the record, and what a value is kept
# as when it is kept as it was given.
ABSENT: Any = object()
AS_GIVEN: Any = object()


@dataclass(frozen=True, slots=True)
class Refused:
    """The record with ``value`` at ``field``, which the kind refuses for that field alone."""

    field: str
    value: Any


@dataclass(frozen=True, slots=True)
class Kept:
    """The record with ``value`` at ``field``, which the kind admits, keeping ``kept`` there."""

    field: str
    value: Any
    kept: Any = AS_GIVEN
