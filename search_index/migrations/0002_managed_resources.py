from django.db import migrations


SCOPE = "infrastructure.resources"
FIELDS = ("key", "kind", "spec", "status", "conditions")


def populate_managed_resources(apps, schema_editor):
    ManagedResource = apps.get_model("control_plane", "ManagedResource")
    SearchDocument = apps.get_model("search_index", "SearchDocument")
    documents = []
    for instance in ManagedResource.objects.all().iterator(chunk_size=500):
        body = "\n".join(
            str(value)
            for field in FIELDS
            if (value := getattr(instance, field, "")) not in (None, "")
        )
        documents.append(
            SearchDocument(scope=SCOPE, object_id=instance.key, body=body)
        )
    SearchDocument.objects.bulk_create(
        documents, batch_size=500, ignore_conflicts=True
    )


def remove_managed_resources(apps, schema_editor):
    apps.get_model("search_index", "SearchDocument").objects.filter(
        scope=SCOPE
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0012_hq_owns_the_topology"),
        ("search_index", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(populate_managed_resources, remove_managed_resources)
    ]
