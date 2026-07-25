from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("control_plane", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="operationrequest",
            name="attempt_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="operationrequest",
            name="lease_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="operationrequest",
            constraint=models.UniqueConstraint(
                condition=models.Q(state__in=("queued", "claimed")),
                fields=("resource", "action"),
                name="one_active_operation_per_resource_action",
            ),
        ),
    ]
