from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0003_topologysnapshot"),
    ]

    operations = [
        migrations.AddField(
            model_name="managedresource",
            name="declaration_source",
            field=models.CharField(
                choices=[("manual", "Manual"), ("topology", "Topology")],
                default="manual",
                max_length=20,
            ),
        ),
    ]
