from django.db import migrations

# The fields a Compose deploy procedure carried before apps were released by
# their own repository's pull. AdoptedProcedure forbids a field it does not
# declare, so a catalog that still holds them cannot be read.
RETIRED = ("compose_updates", "compose_images")


def drop_retired_fields(apps, schema_editor):
    Catalog = apps.get_model("control_plane", "AdoptedCatalog")
    for catalog in Catalog.objects.all():
        procedures = [
            {key: value for key, value in procedure.items() if key not in RETIRED} for procedure in catalog.procedures
        ]
        if procedures != catalog.procedures:
            catalog.procedures = procedures
            catalog.save(update_fields=["procedures"])


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0069_a_pull_request_can_be_merged"),
    ]

    operations = [
        migrations.RunPython(drop_retired_fields, migrations.RunPython.noop),
    ]
