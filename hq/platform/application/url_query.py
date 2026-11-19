"""The one way HQ adds a query to an address it links, keeping the query it holds and every value of a sequence.

Django's ``reverse(query=...)`` serves only an address it reverses, and
Starlette's ``include_query_params`` loses a sequence's other values.
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
