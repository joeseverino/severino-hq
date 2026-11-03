import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0036_choices_read_on_first_use"),
    ]

    operations = [
        migrations.CreateModel(
            name="SealedSecret",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("slot", models.CharField(max_length=200)),
                ("sealed", models.TextField()),
                ("version", models.PositiveIntegerField(default=1)),
                ("set_at", models.DateTimeField()),
                ("set_by", models.CharField(max_length=160)),
                (
                    "resource",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="sealed_secrets",
                        to="control_plane.managedresource",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(fields=("resource", "slot"), name="one_sealed_secret_per_slot")
                ],
            },
        ),
        # No uploaded certificate is held anywhere, so the sealed pair goes
        # without being carried over.
        migrations.RemoveField(model_name="certificatematerial", name="sealed_fullchain"),
        migrations.RemoveField(model_name="certificatematerial", name="sealed_private_key"),
        migrations.AddField(
            model_name="certificatematerial",
            name="fullchain",
            field=models.TextField(default=""),
            preserve_default=False,
        ),
    ]
