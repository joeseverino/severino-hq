"""The full-text entry is rewritten only when the body changes.

An update that leaves ``body`` as it stands touches no full-text segment. Only
the trigger is replaced: no row is read or written.
"""

from django.db import migrations

_REWRITE = (
    "BEGIN INSERT INTO search_index_fts(search_index_fts, rowid, body) "
    "VALUES ('delete', old.id, old.body); "
    "INSERT INTO search_index_fts(rowid, body) VALUES (new.id, new.body); END"
)
_TABLE = "search_index_searchdocument"

CONDITIONAL = (
    f"CREATE TRIGGER search_document_au AFTER UPDATE OF body ON {_TABLE} "
    f"WHEN old.body IS NOT new.body {_REWRITE}"
)
UNCONDITIONAL = f"CREATE TRIGGER search_document_au AFTER UPDATE ON {_TABLE} {_REWRITE}"


def _replace(schema_editor, statement: str) -> None:
    if schema_editor.connection.vendor != "sqlite":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("DROP TRIGGER IF EXISTS search_document_au")
        cursor.execute(statement)


def conditional(apps, schema_editor):
    _replace(schema_editor, CONDITIONAL)


def unconditional(apps, schema_editor):
    _replace(schema_editor, UNCONDITIONAL)


class Migration(migrations.Migration):
    dependencies = [("search_index", "0003_readable_search_bodies")]

    operations = [migrations.RunPython(conditional, unconditional)]
