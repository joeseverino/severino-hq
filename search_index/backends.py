"""Swappable indexed-search backend used by the table query engine."""

from __future__ import annotations

import shlex
from typing import Protocol

from django.db import connection


class SearchBackend(Protocol):
    def search(self, *, scope: str, query: str, limit: int) -> list[str]: ...

    def search_sql(
        self, *, scope: str, query: str, limit: int
    ) -> tuple[str, list] | None: ...


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


search_backend: SearchBackend = SQLiteFTS5Backend()
