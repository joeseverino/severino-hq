from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0016_machines_stop_authoring_the_tailnet_reading")
    ]

    operations = [
        migrations.CreateModel(
            name="DashboardConfiguration",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(
                        default=1, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now, editable=False
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "infrastructure_label",
                    models.CharField(default="Homelab", max_length=40),
                ),
                ("weather_point", models.CharField(blank=True, max_length=64)),
                (
                    "weather_label",
                    models.CharField(default="Weather", max_length=40),
                ),
                (
                    "machine",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="dashboard_configuration",
                        to="control_plane.managedresource",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="DashboardRefreshRequest",
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
                ("panel_id", models.SlugField(max_length=80, unique=True)),
                (
                    "requested_at",
                    models.DateTimeField(default=django.utils.timezone.now),
                ),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
            ],
        ),
        migrations.CreateModel(
            name="WeatherObservation",
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
                ("point", models.CharField(max_length=64, unique=True)),
                ("payload", models.JSONField(default=dict)),
                ("observed_at", models.DateTimeField()),
            ],
        ),
    ]
