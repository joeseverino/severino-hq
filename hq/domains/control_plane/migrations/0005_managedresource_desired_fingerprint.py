from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("control_plane", "0004_managedresource_declaration_source")]

    operations = [
        migrations.AddField(
            model_name="managedresource",
            name="desired_fingerprint",
            field=models.CharField(blank=True, default="", max_length=64),
        )
    ]
