"""Read once per request, and derive once per change, for an extension's own pages (docs/Plugins.md, Derived reads).

``read_once`` shares a read within a request, ``derivation`` keeps a fact until
a table it reads is written, and ``freshness`` judges a reading by its kind's
``ReadCadence``.
"""

from collections.abc import Callable
from typing import Any

from hq.platform.application import derivations as _derivations

# A derivation that asks one of these is derived again at the moment its answer would change.
from hq.platform.application.derivations import reached, today, whole

# A reading's state (current, due, stale or never) and the cadence it is judged by.
from hq.platform.application.freshness import Freshness, ReadCadence, freshness

# A read_once key starts with the extension's own name and holds every argument
# the answer depends on; outside a request it loads every time. day_span turns a
# run of local days into the moments to filter a datetime column by, so the read
# uses the column's index.
from hq.platform.application.projection import day_span, read_once
lazy from hq.platform.application.plugins import provided_under


def derivation[**P, T](
    name: str, *, reads: tuple[str, ...], vary: Callable[..., Any] | None = None
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Declare an extension's function as a derived fact of the tables it reads.

    The host's own, which learns what it reads as it runs: any table beyond
    ``reads``, and the clock, a day holding the answer until local midnight and
    an instant for a minute. The name starts with the extension's own; ``reads``
    names the models it queries; its arguments are plain values, or ``vary``
    says what of them the answer depends on; what it returns must pickle.
    """

    return _derivations.derivation(name, reads=reads, vary=vary, learns=True)


def derive_after_commit(plugin_id: str) -> None:
    """Derive every derivation of the extension asked with no arguments once
    the transaction in progress commits: its own, named under its id, and the
    host's of its providers.

    For a write that changes them all at once, such as an import, so no request
    after it derives one.
    """

    _derivations.derive_after_commit(f"{plugin_id}.", provided_under(plugin_id))


__all__ = [
    "Freshness",
    "ReadCadence",
    "day_span",
    "derivation",
    "derive_after_commit",
    "freshness",
    "reached",
    "read_once",
    "today",
    "whole",
]
