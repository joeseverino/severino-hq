"""An operation's person is a key or blank, as an approval's is."""

from django.db import migrations, models


def _blank(apps, _schema_editor):
    apps.get_model("control_plane", "OperationRequest").objects.filter(requested_person_key__isnull=True).update(
        requested_person_key=""
    )


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0065_a_rule_is_called_as_the_policy_page_calls_it"),
    ]

    operations = [
        migrations.RunPython(_blank, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="operationrequest",
            name="requested_person_key",
            field=models.CharField(blank=True, max_length=100),
        ),
    ]
