"""HQ became the only author of desired state, so provenance stopped varying.

Every existing row was materialised from the topology document. Dropping the
column is what adopts them: nothing is topology-declared any more because
nothing can be, and the import no longer creates or disables resources.

No data is lost that anything could act on. The column recorded which of two
authors wrote the row, and there is now one.
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
