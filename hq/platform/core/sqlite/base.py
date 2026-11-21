"""HQ's database engine: Django's SQLite backend with the schema editor below."""

from typing import override

from django.db.backends.sqlite3 import base, schema


class DatabaseSchemaEditor(schema.DatabaseSchemaEditor):
    @override
    def add_field(self, model, field):
        # SQLite refuses ALTER TABLE ADD COLUMN of a STORED column on a table
        # with rows (sqlite.org/gencol.html), and Django takes that path for a
        # nullable field with no default. Remaking the table, as Django does for
        # every column ALTER TABLE cannot add, computes the column for each row.
        if field.generated and field.db_persist:
            self._remake_table(model, create_field=field)
        else:
            super().add_field(model, field)


class DatabaseWrapper(base.DatabaseWrapper):
    SchemaEditorClass = DatabaseSchemaEditor
