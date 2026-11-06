"""The device an agent asked from, kept on what it asked for, so a block on that device holds it."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0044_capability_rule_per_agent"),
    ]

    operations = [
        migrations.AddField(
            model_name="approvalrequest",
            name="requested_device",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="operationrequest",
            name="requested_device",
            field=models.CharField(blank=True, max_length=64),
        ),
    ]
