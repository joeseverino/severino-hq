"""Swappable indexed-search backend used by the table query engine."""

import shlex
from collections.abc import Sequence
from typing import Protocol

from django.db import connection
from django.db.models import Func, IntegerField
from django.db.models.expressions import Expression

SnippetParts = list[tuple[str, bool]]


class SearchBackend(Protocol):
    """Ranked hits from the index.

    One order holds everywhere: best rank first, and hits of equal rank by
    object id, compared as text. A hit's position in its scope is therefore
    the same whether it is read for one scope, for several, or as a list's
    relevance column.
    """

    def search(self, *, scope: str, query: str, limit: int) -> list[str]:
        """Return relevance-ordered stable object ids for one scope."""

    def search_scopes(
        self, *, scopes: Sequence[str], query: str, limit: int
    ) -> dict[str, list[tuple[str, SnippetParts]]]:
        """Return each scope's first ``limit`` hits as (object id, snippet parts).

        Only the named scopes are read. A scope with no hit has no key.
        """

    def position(
        self, *, scope: str, query: str, limit: int, identifier: Expression
    ) -> Expression | None:
        """Return an expression for a row's 1-based position among the hits.

        ``identifier`` is the row's object id as text. The expression is NULL
        for a row that is not among the first ``limit`` hits, so one
        annotation both filters a queryset and orders it. None for an empty
        query.
        """


def _fts_query(query: str) -> str:
    try:
        terms = shlex.split(query)
    except ValueError:
        terms = query.split()
    escaped = [term.replace('"', '""') for term in terms[:8] if term]
    return " AND ".join(f'"{term}"*' for term in escaped)


# The one statement that ranks. ``rank`` is FTS5's bm25 score, lower is
# better; object id breaks a tie, so the order is total and does not depend on
# how the index happens to store its rows. An FTS5 auxiliary function is only
# valid in the query that reads the FTS table with MATCH, which is why the
# rank is read here and a snippet is read by a second, plain reference.
_RANKED_SQL = """
    SELECT search.rowid AS id,
           document.scope AS scope,
           document.object_id AS object_id,
           ROW_NUMBER() OVER (
               PARTITION BY document.scope
               ORDER BY search.rank, document.object_id
           ) AS position
    FROM search_index_fts AS search
    JOIN search_index_searchdocument AS document
      ON document.id = search.rowid
    WHERE search_index_fts MATCH %s AND document.scope IN ({scopes})
"""

_IDS_SQL = """
    SELECT ranked.object_id
    FROM ({ranked}) AS ranked
    WHERE ranked.position <= %s
    ORDER BY ranked.position
"""

# CROSS JOIN fixes the loop order: the match is walked once and each row is
# probed against the few kept hits, rather than the match being reopened once
# per kept hit.
_SNIPPETS_SQL = """
    SELECT ranked.scope,
           ranked.object_id,
           snippet(search_index_fts, 0, char(2), char(3), ' … ', 16)
    FROM search_index_fts
    CROSS JOIN (
        SELECT * FROM ({ranked}) WHERE position <= %s
    ) AS ranked
    WHERE search_index_fts MATCH %s AND ranked.id = search_index_fts.rowid
    ORDER BY ranked.scope, ranked.position
"""

# A scalar subquery per row, over hits that are ranked once: MATERIALIZED
# keeps the ranking out of the per-row loop.
_POSITION_SQL = """(
    WITH ranked AS MATERIALIZED ({ranked})
    SELECT ranked.position
    FROM ranked
    WHERE ranked.object_id = {identifier} AND ranked.position <= %s
)"""


def _ranked_sql(scopes: Sequence[str]) -> str:
    """The ranking statement for this many scopes; only placeholders vary."""
    return _RANKED_SQL.format(scopes=", ".join(["%s"] * len(scopes)))


class IndexPosition(Func):
    """A row's position among the ranked hits of one scope, NULL when absent."""

    def __init__(
        self,
        identifier: Expression,
        *,
        expression: str,
        scope: str,
        limit: int,
    ) -> None:
        super().__init__(identifier, output_field=IntegerField())
        self.expression = expression
        self.scope = scope
        self.limit = limit

    def as_sql(self, compiler, connection, **extra_context):
        (source,) = self.get_source_expressions()
        identifier, identifier_params = compiler.compile(source)
        sql = _POSITION_SQL.format(
            ranked=_ranked_sql([self.scope]), identifier=identifier
        )
        return sql, (self.expression, self.scope, *identifier_params, self.limit)


# snippet() marks matched tokens with control characters that cannot appear
# in the indexed text, so the split into (text, is_match) parts is unambiguous
# and the renderer escapes text and markup independently.
_MATCH_START = "\x02"
_MATCH_END = "\x03"


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
    def search(self, *, scope: str, query: str, limit: int) -> list[str]:
        expression = _fts_query(query)
        if not expression:
            return []
        sql = _IDS_SQL.format(ranked=_ranked_sql([scope]))
        with connection.cursor() as cursor:
            cursor.execute(sql, [expression, scope, limit])
            return [row[0] for row in cursor.fetchall()]

    def search_scopes(
        self, *, scopes: Sequence[str], query: str, limit: int
    ) -> dict[str, list[tuple[str, SnippetParts]]]:
        expression = _fts_query(query)
        if not expression or not scopes:
            return {}
        sql = _SNIPPETS_SQL.format(ranked=_ranked_sql(scopes))
        hits: dict[str, list[tuple[str, SnippetParts]]] = {}
        with connection.cursor() as cursor:
            cursor.execute(sql, [expression, *scopes, limit, expression])
            for scope, object_id, raw in cursor.fetchall():
                hits.setdefault(scope, []).append((object_id, snippet_parts(raw)))
        return hits

    def position(
        self, *, scope: str, query: str, limit: int, identifier: Expression
    ) -> Expression | None:
        expression = _fts_query(query)
        if not expression:
            return None
        return IndexPosition(
            identifier, expression=expression, scope=scope, limit=limit
        )


search_backend: SearchBackend = SQLiteFTS5Backend()
