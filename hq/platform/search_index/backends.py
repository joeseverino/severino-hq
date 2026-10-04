"""Swappable indexed-search backend used by the table query engine."""

from __future__ import annotations

import shlex
from typing import Protocol

from django.db import connection


SnippetParts = list[tuple[str, bool]]


class SearchBackend(Protocol):
    def search(self, *, scope: str, query: str, limit: int) -> list[str]:
        """Return relevance-ordered stable object ids for one scope."""

    def search_sql(
        self, *, scope: str, query: str, limit: int
    ) -> tuple[str, list] | None:
        """Return a parameterized id subquery, or None for an empty query."""

    def search_snippets(
        self, *, scope: str, query: str, limit: int
    ) -> list[tuple[str, SnippetParts]]:
        """Return (object_id, snippet parts) pairs; parts flag matched text."""


def _fts_query(query: str) -> str:
    try:
        terms = shlex.split(query)
    except ValueError:
        terms = query.split()
    escaped = [term.replace('"', '""') for term in terms[:8] if term]
    return " AND ".join(f'"{term}"*' for term in escaped)


_MATCH_SQL = """
    SELECT document.object_id
    FROM search_index_fts AS search
    JOIN search_index_searchdocument AS document
      ON document.id = search.rowid
    WHERE search_index_fts MATCH %s AND document.scope = %s
    ORDER BY search.rank
    LIMIT %s
"""

# snippet() marks matched tokens with control characters that cannot appear
# in the indexed text, so the split into (text, is_match) parts is unambiguous
# and the renderer escapes text and markup independently.
_MATCH_START = "\x02"
_MATCH_END = "\x03"

_SNIPPET_SQL = """
    SELECT document.object_id,
           snippet(search_index_fts, 0, char(2), char(3), ' … ', 16)
    FROM search_index_fts AS search
    JOIN search_index_searchdocument AS document
      ON document.id = search.rowid
    WHERE search_index_fts MATCH %s AND document.scope = %s
    ORDER BY search.rank
    LIMIT %s
"""


def snippet_parts(raw: str) -> SnippetParts:
    """Split marker-delimited snippet text into (text, is_match) parts."""
    parts: SnippetParts = []
    for chunk in raw.replace("\n", " ").split(_MATCH_START):
        if _MATCH_END in chunk:
            match, rest = chunk.split(_MATCH_END, 1)
            if match:
                parts.append((match, True))
            if rest:
                parts.append((rest, False))
        elif chunk:
            parts.append((chunk, False))
    return parts


class SQLiteFTS5Backend:
    def search_sql(
        self, *, scope: str, query: str, limit: int
    ) -> tuple[str, list] | None:
        """Parameterized subquery form, usable inside an ORM ``__in`` filter."""
        expression = _fts_query(query)
        if not expression:
            return None
        return _MATCH_SQL, [expression, scope, limit]

    def search(self, *, scope: str, query: str, limit: int) -> list[str]:
        sql_params = self.search_sql(scope=scope, query=query, limit=limit)
        if sql_params is None:
            return []
        with connection.cursor() as cursor:
            cursor.execute(*sql_params)
            return [row[0] for row in cursor.fetchall()]

    def search_snippets(
        self, *, scope: str, query: str, limit: int
    ) -> list[tuple[str, SnippetParts]]:
        expression = _fts_query(query)
        if not expression:
            return []
        with connection.cursor() as cursor:
            cursor.execute(_SNIPPET_SQL, [expression, scope, limit])
            return [(row[0], snippet_parts(row[1])) for row in cursor.fetchall()]


search_backend: SearchBackend = SQLiteFTS5Backend()
