from django.db import migrations


def read_digests_again(apps, _schema_editor):
    """Drop every digest's reading, so the next refresh reads each again with
    the main modules its SBOM names: a digest's reading is otherwise never
    read twice."""

    apps.get_model("control_plane", "ProviderInventory").objects.filter(kind="registry.digest").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0074_a_scan_names_its_repository"),
    ]

    operations = [
        migrations.RunPython(read_digests_again, migrations.RunPython.noop),
    ]
