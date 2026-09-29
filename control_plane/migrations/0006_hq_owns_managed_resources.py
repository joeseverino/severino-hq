"""Drop the provenance column: HQ is the only author of desired state.

Dropping the column adopts every existing row as HQ's own. No data is lost that
anything could act on: the column recorded which of two authors wrote the row,
and there is one.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0005_managedresource_desired_fingerprint"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="managedresource",
            name="declaration_source",
        ),
    ]
