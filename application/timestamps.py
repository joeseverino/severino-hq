"""One parser for a timestamp someone else wrote, with its edges stated.

HQ reads instants from providers, from its own serialized projections and from
stored records. Parsing them was written about twenty times, and the copies
agreed on the common case and disagreed at the edges, silently. Those edges
are parameters here, so a caller states which answer it needs rather than
inheriting whichever one its copy happened to have:

- **Naive input.** ``naive="utc"`` reads a stamp without an offset as UTC,
  which is what every provider that omits it means. ``"keep"`` returns it
  naive, for a caller comparing it only with other stamps from the same
  source. ``"refuse"`` answers ``None``, for a caller that must not guess.
- **The zero time.** Go (and so Tailscale) writes ``0001-01-01T00:00:00Z``
  for "never". Read as an instant it is two thousand years ago and looks like
  a bug rather than a fact, so ``zero_is_never`` (the default) answers
  ``None``. No source HQ reads uses year 1 for anything else.
- **An already-parsed value.** A ``datetime`` is taken as given, and the naive
  and zero-time rules still apply to it.

Anything unparseable, empty or missing is ``None``: a reading is a fact about
the world, not an invariant of ours. A caller for whom a malformed stamp is an
error checks for ``None`` and raises its own.

A trailing ``Z`` needs no rewriting: ``datetime.fromisoformat`` accepts it on
every Python HQ supports.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal

Naive = Literal["utc", "keep", "refuse"]


def moment(
    stamp: object,
    *,
    naive: Naive = "utc",
    zero_is_never: bool = True,
) -> datetime | None:
    """``stamp`` as a datetime, or ``None`` when it does not name an instant."""

    found = stamp if isinstance(stamp, datetime) else _parsed(stamp)
    if found is None or (zero_is_never and found.date() == date.min):
        return None
    if found.tzinfo is not None or naive == "keep":
        return found
    if naive == "refuse":
        return None
    return found.replace(tzinfo=timezone.utc)


def _parsed(stamp: object) -> datetime | None:
    text = str(stamp or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
