"""Primitives every read projection needs, declared once.

Small on purpose, and deliberately dependency-free: this is imported by modules
that serialize projects, assets, content, expenses and infrastructure, and a
shared module that reached for any of their models would make importing one of
them mean importing all of them.

What lives here is the handful of rules that were copied instead. Paging bounds
had four spellings and the ceiling itself had three definitions plus one inline
literal, which is how a limit gets raised in one place and quietly not in the
others. A timestamp had four identical renderers, which is one per surface that
would have to be found on the day an API is asked for something other than ISO.
"""

from __future__ import annotations

from typing import Any

# The most rows any single read will return, whatever a caller asks for.
#
# A ceiling rather than a suggestion: these projections are reached from the
# web, the API and the MCP, and the last of those is driven by a model that will
# cheerfully ask for a million records and then try to read them.
MAX_PAGE_SIZE = 100


def page_size(limit: int, *, maximum: int = MAX_PAGE_SIZE) -> int:
    """How many rows to actually return for a requested limit.

    Rejects nonsense rather than silently correcting it: a caller asking for
    zero or a negative page has a bug, and quietly handing back the default
    hides it behind a page that looks fine.
    """

    if limit < 1:
        raise ValueError("limit must be at least 1")
    return min(limit, maximum)


def iso(value: Any) -> str | None:
    """A timestamp as a contract renders it, or None when there is none.

    None rather than an empty string, because these become JSON: a consumer can
    test for null, while "" is a value that has to be special-cased at every
    call site to mean absent.
    """

    return value.isoformat() if value else None
