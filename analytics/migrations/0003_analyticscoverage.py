from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("analytics", "0002_site_grain_and_location_index")]

    operations = [
        migrations.CreateModel(
            name="AnalyticsCoverage",
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
                ("date", models.DateField()),
                (
                    "site",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="coverage",
                        to="analytics.analyticssite",
                    ),
                ),
            ],
            options={
                "ordering": ("-date",),
                "indexes": [
                    models.Index(fields=["date"], name="analytics_a_date_5615ed_idx")
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("site", "date"),
                        name="analytics_coverage_unique_day",
                    )
                ],
            },
        )
    ]
