"""Who asked for an operation is read from its audit event, never a user column."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0063_an_address_reading_is_an_upstream_reading"),
    ]

    operations = [
        migrations.RemoveField(model_name="operationrequest", name="requested_by"),
    ]
