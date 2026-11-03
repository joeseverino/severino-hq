"""The retry-key record leaves hq_api's state for core's: state only, so its table stays."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("hq_api", "0001_initial"),
        ("core", "0028_idempotency_record"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.DeleteModel(name="IdempotencyRecord")],
            database_operations=[],
        ),
    ]
