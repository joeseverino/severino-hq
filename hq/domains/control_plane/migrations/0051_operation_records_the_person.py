"""Keep a queued operation's agent scoped to its person without guessing old attribution."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("control_plane", "0050_acronym_names")]

    operations = [
        migrations.AddField(
            model_name="operationrequest",
            name="requested_person_key",
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
    ]
