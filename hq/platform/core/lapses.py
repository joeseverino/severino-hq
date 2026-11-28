"""When a live record has lapsed, declared once on its model.

A page filters by the lapse in SQL and a service asks it of one row, both from
the one ``Lapse``: a specification, as Evans names the pattern.
"""

import operator
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import reduce
from typing import Any

from django.db.models import Min, Q, QuerySet


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

    def next(self, rows: QuerySet, now: datetime) -> datetime | None:
        """The earliest moment one of ``rows`` not lapsed at ``now`` lapses;
        None when none can."""

        moments = (
            (rows.filter(state=state, **{f"{moment}__gt": now - grace}).aggregate(at=Min(moment))["at"], grace)
            for state, moment, grace in self.terms
        )
        return min((at + grace for at, grace in moments if at is not None), default=None)

    def holds(self, row: Any, now: datetime) -> bool:
        """Whether ``row`` has lapsed at ``now``."""

        return any(
            row.state == state and (at := getattr(row, moment)) is not None and at <= now - grace
            for state, moment, grace in self.terms
        )
