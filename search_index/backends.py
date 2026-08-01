"""Swappable indexed-search backend used by the table query engine."""

from __future__ import annotations

import shlex
from typing import Protocol

from django.db import connection


class SearchBackend(Protocol):
    def search(self, *, scope: str, query: str, limit: int) -> list[str]: ...


def _fts_query(query: str) -> str:
    try:
        terms = shlex.split(query)
    except ValueError:
        terms = query.split()
    escaped = [term.replace('"', '""') for term in terms[:8] if term]
    return " AND ".join(f'"{term}"*' for term in escaped)


class SQLiteFTS5Backend:
    def search(self, *, scope: str, query: str, limit: int) -> list[str]:
        expression = _fts_query(query)
        if not expression:
            return []
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT document.object_id
                FROM search_index_fts AS search
                JOIN search_index_searchdocument AS document
                  ON document.id = search.rowid
                WHERE search_index_fts MATCH %s AND document.scope = %s
                ORDER BY search.rank
                LIMIT %s
                """,
                [expression, scope, limit],
            )
            return [row[0] for row in cursor.fetchall()]


search_backend: SearchBackend = SQLiteFTS5Backend()
