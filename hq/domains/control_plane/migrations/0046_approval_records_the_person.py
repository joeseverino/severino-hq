"""The person a token asked for, kept on what it asked for, so an approved personal write replays as theirs."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0045_approval_and_operation_record_the_device"),
    ]

    operations = [
        migrations.AddField(
            model_name="approvalrequest",
            name="requested_person_key",
            field=models.CharField(blank=True, max_length=100),
        ),
    ]
