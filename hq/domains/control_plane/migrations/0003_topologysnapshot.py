from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [("control_plane", "0002_operation_leases_and_active_constraint")]

    operations = [
        migrations.CreateModel(
            name="TopologySnapshot",
            fields=[
                (
                    "created_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now, editable=False
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "id",
                    models.CharField(
                        default="topology", max_length=64, primary_key=True, serialize=False
                    ),
                ),
                ("schema_version", models.PositiveIntegerField()),
                ("checksum", models.CharField(max_length=64)),
                ("payload", models.JSONField()),
            ],
        )
    ]
