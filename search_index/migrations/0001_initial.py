import sqlite3

from django.db import migrations, models


DEFINITIONS = (
    ("projects", "Project", "projects", "slug", ("name", "slug", "description", "technologies_used", "notes")),
    ("assets", "Asset", "assets", "slug", ("item_name", "slug", "vendor", "serial_number", "category", "notes")),
    ("content", "ContentItem", "content", "slug", ("title", "slug", "topic", "tags", "notes")),
    ("docs_index", "DocumentationRecord", "documentation", "doc_id", ("doc_id", "title", "system_service", "obsidian_path", "github_path", "notes")),
    ("expenses", "Expense", "expenses", "pk", ("vendor", "item", "category", "business_purpose", "notes")),
    ("receipts", "Receipt", "receipts", "pk", ("original_filename", "vendor", "notes")),
    ("core", "AuditLog", "audit", "pk", ("action", "object_type", "object_id", "object_repr", "message")),
)


def populate_search_documents(apps, schema_editor):
    SearchDocument = apps.get_model("search_index", "SearchDocument")
    documents = []
    for app_label, model_name, scope, identifier_field, fields in DEFINITIONS:
        model = apps.get_model(app_label, model_name)
        for instance in model.objects.all().iterator(chunk_size=500):
            body = "\n".join(
                str(value)
                for field in fields
                if (value := getattr(instance, field, "")) not in (None, "")
            )
            documents.append(
                SearchDocument(
                    scope=scope,
                    object_id=str(getattr(instance, identifier_field)),
                    body=body,
                )
            )
    SearchDocument.objects.bulk_create(documents, batch_size=500)


def create_fts5(schema_editor):
    if schema_editor.connection.vendor != "sqlite":
        return
    statements = (
        "CREATE VIRTUAL TABLE search_index_fts USING fts5(body, content='search_index_searchdocument', content_rowid='id', tokenize='unicode61 remove_diacritics 2', prefix='2 3 4')",
        "CREATE TRIGGER search_document_ai AFTER INSERT ON search_index_searchdocument BEGIN INSERT INTO search_index_fts(rowid, body) VALUES (new.id, new.body); END",
        "CREATE TRIGGER search_document_ad AFTER DELETE ON search_index_searchdocument BEGIN INSERT INTO search_index_fts(search_index_fts, rowid, body) VALUES ('delete', old.id, old.body); END",
        "CREATE TRIGGER search_document_au AFTER UPDATE ON search_index_searchdocument BEGIN INSERT INTO search_index_fts(search_index_fts, rowid, body) VALUES ('delete', old.id, old.body); INSERT INTO search_index_fts(rowid, body) VALUES (new.id, new.body); END",
    )
    # Without secure-delete, FTS5 only marks deleted rows; their text lingers
    # in index segments until a merge. The audit/expense scopes make that a
    # data-retention leak, so scrub on delete where the runtime supports it
    # (SQLite >= 3.42). The flag persists in the table's FTS config, so
    # setting it once at creation covers the table's lifetime.
    if sqlite3.sqlite_version_info >= (3, 42, 0):
        statements += (
            "INSERT INTO search_index_fts(search_index_fts, rank) VALUES ('secure-delete', 1)",
        )
    with schema_editor.connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)


def drop_fts5(schema_editor):
    if schema_editor.connection.vendor != "sqlite":
        return
    with schema_editor.connection.cursor() as cursor:
        for name in ("search_document_au", "search_document_ad", "search_document_ai"):
            cursor.execute(f"DROP TRIGGER IF EXISTS {name}")
        cursor.execute("DROP TABLE IF EXISTS search_index_fts")


def create_fts5_migration(apps, schema_editor):
    create_fts5(schema_editor)


def drop_fts5_migration(apps, schema_editor):
    drop_fts5(schema_editor)


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("assets", "0001_initial"),
        ("content", "0003_collapse_status_and_add_page_type"),
        ("core", "0001_initial"),
        ("docs_index", "0006_alter_documentationrecord_status"),
        ("expenses", "0001_initial"),
        ("projects", "0002_project_last_push_at"),
        ("receipts", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="SearchDocument",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("scope", models.CharField(max_length=40)),
                ("object_id", models.CharField(max_length=200)),
                ("body", models.TextField()),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "indexes": [models.Index(fields=["scope", "object_id"], name="search_idx_scope_obj")],
                "constraints": [models.UniqueConstraint(fields=("scope", "object_id"), name="search_document_scope_object_unique")],
            },
        ),
        migrations.RunPython(create_fts5_migration, drop_fts5_migration),
        migrations.RunPython(populate_search_documents, migrations.RunPython.noop),
    ]
