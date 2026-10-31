"""Read once per request, and derive once per change, for an extension's own pages.

``read_once`` shares one read among the functions assembling a page, and
``derivation`` keeps a fact of the extension's own across requests until a
table it reads is written. The rules an answer keeps, the clock among them:
docs/Plugins.md, Derived reads.
"""

from collections.abc import Callable
from typing import Any

from hq.platform.application import derivations as _derivations

# present is the instant the others compare against; a derivation that asks one
# of the others is derived again at the moment its answer would change.
from hq.platform.application.derivations import (
    passed,
    present,
    reached,
    since,
    today,
    whole,
)

# A read_once key starts with the extension's own name and holds every argument
# the answer depends on; outside a request it loads every time. day_span turns a
# run of local days into the moments to filter a datetime column by, so the read
# uses the column's index.
from hq.platform.application.projection import day_span, read_once


def derivation[**P, T](
    name: str, *, reads: tuple[str, ...], vary: Callable[..., Any] | None = None
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Declare an extension's function as a derived fact of the tables it reads.

    The host's own, declared as one whose reads of the clock it cannot see. The
    name starts with the extension's own; ``reads`` names the models it queries,
    and any beyond them is learned; its arguments are plain values, or ``vary``
    says what of them the answer depends on; what it returns must pickle.
    """

    return _derivations.derivation(name, reads=reads, vary=vary, unseen=True)


__all__ = [
    "day_span",
    "derivation",
    "passed",
    "present",
    "reached",
    "read_once",
    "since",
    "today",
    "whole",
]
