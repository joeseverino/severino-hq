"""An agent's row is one client as one person: its person's sign-in key is on
the row, read from the sign-in that bound them, and a client id alone no longer
names one row.
"""

from django.db import migrations, models

SIGN_IN = "oidc"


def key_each_person(apps, schema_editor):
    AgentIdentity = apps.get_model("core", "AgentIdentity")
    LinkedAccount = apps.get_model("core", "LinkedAccount")
    for row in AgentIdentity.objects.exclude(person=None):
        key = (
            LinkedAccount.objects.filter(user_id=row.person_id, provider=SIGN_IN)
            .values_list("login", flat=True)
            .first()
        )
        if key:
            row.person_key = key
            row.save(update_fields=["person_key"])


class Migration(migrations.Migration):
    dependencies = [("core", "0032_agent_devices")]

    operations = [
        migrations.AddField(
            model_name="agentidentity",
            name="person_key",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.RunPython(key_each_person, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="agentidentity",
            name="client_id",
            field=models.CharField(max_length=160),
        ),
        migrations.AlterModelOptions(
            name="agentidentity",
            options={"ordering": ("client_id", "person_key")},
        ),
    ]
