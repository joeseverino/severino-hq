"""A capability rule's choices are called as the policy page calls them."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0064_who_asked_for_an_operation_is_its_audit_event"),
    ]

    operations = [
        migrations.AlterField(
            model_name="capabilityrule",
            name="rule",
            field=models.CharField(
                choices=[("allow", "On"), ("approve", "Ask me first"), ("deny", "Off")], max_length=16
            ),
        ),
    ]
