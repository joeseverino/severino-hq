"""When a live record has lapsed, declared once on its model.

A record that waits on something outside it (a job's thread, a worker's
lease, a person's decision) lapses once that has gone quiet too long, whether
or not a sweep has marked it yet. A page filters by the lapse in SQL and a
service asks it of one row in Python, so both read it from the one ``Lapse``
the model declares: a specification, as Evans names the pattern, that
both selects rows and tests one.
"""

import operator
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import reduce
from typing import Any

from django.db.models import Q


@dataclass(frozen=True, slots=True)
class Lapse:
    """A record in one of ``terms``' states has lapsed once its moment, plus
    the grace beside it, has come; a record with no moment has not."""

    # Each ``(state, moment field, grace)``.
    terms: tuple[tuple[str, str, timedelta], ...]

    def q(self, now: datetime) -> Q:
        """The rows lapsed at ``now``."""

        return reduce(
            operator.or_, (Q(state=state, **{f"{moment}__lte": now - grace}) for state, moment, grace in self.terms)
        )

    def holds(self, row: Any, now: datetime) -> bool:
        """Whether ``row`` has lapsed at ``now``."""

        return any(
            row.state == state and (at := getattr(row, moment)) is not None and at <= now - grace
            for state, moment, grace in self.terms
        )
