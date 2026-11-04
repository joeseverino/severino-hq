from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0040_contact_submission_list_retired"),
    ]

    operations = [
        migrations.CreateModel(
            name="RefusedReport",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("action", models.CharField(max_length=64)),
                ("controller_id", models.CharField(blank=True, max_length=160)),
                ("status", models.PositiveSmallIntegerField()),
                ("detail", models.CharField(max_length=500)),
                ("refused_at", models.DateTimeField()),
            ],
            options={
                "ordering": ("action", "controller_id"),
                "constraints": [
                    models.UniqueConstraint(
                        fields=("action", "controller_id"), name="one_refused_report_per_action_controller"
                    )
                ],
            },
        ),
    ]
