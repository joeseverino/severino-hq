"""Re-project every indexed body through the readable formatter.

Rows written before this hold a Python dict where the snippet under a result
gets cut from. Production runs ``migrate`` on every boot and nothing else, so
this is the only place a projection gets rebuilt without an operator
remembering to.

Field lists come from the live registry rather than being restated -- the
previous reindex restated them, which put a body's definition in two places.
Rows come from the historical models, as a migration requires. A scope whose
model is not in this migration's state is skipped; its own saves reindex it.
"""

from django.db import migrations

from application.search_contracts import SearchDefinition


def _definitions() -> tuple[SearchDefinition, ...]:
    # Imported at call time: the registry builds against real models, and
    # nothing about it should run while migrations are merely being loaded.
    from search_index.registry import DEFINITIONS

    return DEFINITIONS


def rebuild_bodies(apps, schema_editor):
    SearchDocument = apps.get_model("search_index", "SearchDocument")
    for definition in _definitions():
        meta = definition.model._meta
        try:
            model = apps.get_model(meta.app_label, meta.model_name)
        except LookupError:
            continue
        # One query for the scope's rows rather than one per record: a lookup
        # inside the loop is the shape this repository measures for.
        existing = {
            document.object_id: document
            for document in SearchDocument.objects.filter(scope=definition.scope)
        }
        updates = []
        for instance in model.objects.all().iterator(chunk_size=500):
            document = existing.get(definition.object_id(instance))
            if document is None:
                continue
            document.body = definition.body(instance)
            updates.append(document)
        SearchDocument.objects.bulk_update(updates, ["body"], batch_size=500)


def noop(apps, schema_editor):
    """Nothing to undo: the previous bodies were a formatting of the same rows."""


class Migration(migrations.Migration):
    dependencies = [
        ("search_index", "0002_managed_resources"),
    ]

    operations = [migrations.RunPython(rebuild_bodies, noop)]
