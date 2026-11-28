"""The index holds a record's title, identifiers and words as columns of their own.

Its rows are emptied here and drawn again from their records once every app is
migrated (``apps.reproject``): the full-text table is replaced, and its old
rows hold all three run together.
"""

import sqlite3

from django.db import migrations, models

_DROPPED = (
    "DROP TRIGGER IF EXISTS search_document_au",
    "DROP TRIGGER IF EXISTS search_document_ad",
    "DROP TRIGGER IF EXISTS search_document_ai",
    "DROP TABLE IF EXISTS search_index_fts",
)
_DELETE = (
    "INSERT INTO search_index_fts(search_index_fts, rowid, title, identifiers, body) "
    "VALUES ('delete', old.id, old.title, old.identifiers, old.body);"
)
_INSERT = "INSERT INTO search_index_fts(rowid, title, identifiers, body) VALUES (new.id, new.title, new.identifiers, new.body);"
_CREATED = (
    (
        "CREATE VIRTUAL TABLE search_index_fts USING fts5(title, identifiers, body, "
        "content='search_index_searchdocument', content_rowid='id', "
        "tokenize='unicode61 remove_diacritics 2', prefix='2 3 4')"
    ),
    "CREATE TRIGGER search_document_ai AFTER INSERT ON search_index_searchdocument BEGIN " + _INSERT + " END",
    "CREATE TRIGGER search_document_ad AFTER DELETE ON search_index_searchdocument BEGIN " + _DELETE + " END",
    # An update that leaves every column as it stands touches no segment.
    (
        "CREATE TRIGGER search_document_au AFTER UPDATE OF title, identifiers, body ON search_index_searchdocument "
        "WHEN old.title IS NOT new.title OR old.identifiers IS NOT new.identifiers OR old.body IS NOT new.body "
        "BEGIN " + _DELETE + " " + _INSERT + " END"
    ),
)
# Deleted text is scrubbed from the segments, not only marked, since the audit
# and expense scopes make lingering text a retention leak (SQLite 3.42 on).
_SECURE = ("INSERT INTO search_index_fts(search_index_fts, rank) VALUES ('secure-delete', 1)",)


def _execute(schema_editor, statements) -> None:
    if schema_editor.connection.vendor != "sqlite":
        return
    with schema_editor.connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)


def drop(apps, schema_editor):
    _execute(schema_editor, _DROPPED)
    apps.get_model("search_index", "SearchDocument").objects.all().delete()


def create(apps, schema_editor):
    _execute(schema_editor, _CREATED + (_SECURE if sqlite3.sqlite_version_info >= (3, 42, 0) else ()))


class Migration(migrations.Migration):
    dependencies = [("search_index", "0007_acronym_names")]

    operations = [
        migrations.RunPython(drop),
        migrations.AddField(model_name="searchdocument", name="title", field=models.TextField(default="")),
        migrations.AddField(model_name="searchdocument", name="identifiers", field=models.TextField(default="")),
        migrations.RunPython(create),
    ]
