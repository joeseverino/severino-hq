"""The one way HQ adds a query to an address it links.

``with_query`` appends to whatever query the address already holds, before
its fragment, and encodes through Django's ``urlencode`` with ``doseq``, so a
sequence is one pair per value and a ``QueryDict`` keeps every value of a key.
Django's own ``reverse(query=...)`` does this only for an address it reverses,
and Starlette's ``URL.include_query_params`` replaces a key and loses the
other values of a sequence, so neither serves an address another part built.
"""

from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from django.http import QueryDict
from django.utils.http import urlencode

type Query = Mapping[str, Any] | Iterable[tuple[str, Any]] | QueryDict


def with_query(url: Any, query: Query | None = None, *, fragment: str | None = None) -> str:
    """``url`` with ``query`` added to its own, and ``fragment`` in place of
    its own when given. An empty ``query`` leaves the address as it was."""

    parts = urlsplit(str(url))
    if isinstance(query, QueryDict):
        added = query.urlencode()
    elif isinstance(query, Mapping):
        added = urlencode(query, doseq=True)
    else:
        added = urlencode(list(query or ()), doseq=True)
    joined = "&".join(part for part in (parts.query, added) if part)
    return urlunsplit(parts._replace(query=joined, fragment=parts.fragment if fragment is None else fragment))
