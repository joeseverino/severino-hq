from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0033_derived_references"),
    ]

    operations = [
        migrations.AddField(
            model_name="providerconnection",
            name="unreported_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
