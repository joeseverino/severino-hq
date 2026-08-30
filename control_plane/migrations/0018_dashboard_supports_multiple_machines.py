from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


def carry_selected_machine(apps, schema_editor):
    configuration = apps.get_model("control_plane", "DashboardConfiguration")
    dashboard_machine = apps.get_model("control_plane", "DashboardMachine")
    selected = configuration.objects.filter(machine_id__isnull=False).first()
    if selected:
        dashboard_machine.objects.get_or_create(
            machine_id=selected.machine_id,
            defaults={"position": 0},
        )


class Migration(migrations.Migration):
    dependencies = [("control_plane", "0017_dashboard_observations")]

    operations = [
        migrations.CreateModel(
            name="DashboardMachine",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now, editable=False
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("position", models.PositiveSmallIntegerField(default=0)),
                (
                    "machine",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="dashboard_placement",
                        to="control_plane.managedresource",
                    ),
                ),
            ],
            options={"ordering": ("position", "pk")},
        ),
        migrations.RunPython(carry_selected_machine, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="dashboardconfiguration",
            name="machine",
        ),
    ]
