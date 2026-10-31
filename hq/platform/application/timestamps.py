"""One parser for a timestamp someone else wrote, with its edges stated.

HQ reads instants from providers, its own serialized projections and stored
records. Callers agree on the common case and differ at the edges (a naive
stamp, Go's zero time), so those edges are parameters and a caller states which
answer it needs. Anything unparseable, empty or missing is ``None``: a reading
is a fact about the world, and a caller for whom that is an error raises its own.
"""

from datetime import UTC, date, datetime
from typing import Literal

# How a stamp without an offset is read: "utc" is what every provider that
# omits one means; "keep" leaves it naive, for a caller comparing it only with
# stamps from the same source; "refuse" answers None, for one that must not guess.
Naive = Literal["utc", "keep", "refuse"]


def moment(
    stamp: object,
    *,
    naive: Naive = "utc",
    zero_is_never: bool = True,
) -> datetime | None:
    """``stamp`` as a datetime, or ``None`` when it does not name an instant."""

    # A datetime is taken as given, and the naive and zero-time rules still
    # apply to it.
    found = stamp if isinstance(stamp, datetime) else _parsed(stamp)
    # Go (and so Tailscale) writes ``0001-01-01T00:00:00Z`` for "never", which
    # read as an instant looks like a bug rather than a fact. No source HQ
    # reads uses year 1 for anything else.
    if found is None or (zero_is_never and found.date() == date.min):
        return None
    if found.tzinfo is not None or naive == "keep":
        return found
    if naive == "refuse":
        return None
    return found.replace(tzinfo=UTC)


def _parsed(stamp: object) -> datetime | None:
    text = str(stamp or "").strip()
    if not text:
        return None
    try:
        # A trailing ``Z`` needs no rewriting: fromisoformat accepts it on
        # every Python HQ supports.
        return datetime.fromisoformat(text)
    except ValueError:
        return None
