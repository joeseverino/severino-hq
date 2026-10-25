from django.db import migrations


def delete_task_docs(apps, schema_editor):
    # The vault keeps no task notes, so each is an orphan, and the importer
    # deletes an orphan when it prunes.
    apps.get_model("docs_index", "DocumentationRecord").objects.filter(doc_type="task").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("docs_index", "0009_doc_type_queue_report"),
    ]

    operations = [
        migrations.RunPython(delete_task_docs, migrations.RunPython.noop),
    ]
