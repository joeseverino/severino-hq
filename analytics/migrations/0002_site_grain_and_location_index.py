from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("analytics", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="analyticssite",
            name="site_tag",
            field=models.CharField(max_length=64),
        ),
        migrations.AddConstraint(
            model_name="analyticssite",
            constraint=models.UniqueConstraint(
                fields=("connection_ref", "site_tag"),
                name="analytics_site_ref_tag_unique",
            ),
        ),
        migrations.AddIndex(
            model_name="rumdaily",
            index=models.Index(
                fields=["site", "dimension", "value", "-date"],
                name="analytics_rum_loc_date_idx",
            ),
        ),
    ]
