"""One row per client and person, on its own, so ``constraint_preflight`` counts the rows it would refuse."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0033_agent_identity_per_person")]

    operations = [
        migrations.AddConstraint(
            model_name="agentidentity",
            constraint=models.UniqueConstraint(
                fields=("client_id", "person_key"), name="agent_identity_one_per_client_and_person"
            ),
        ),
    ]
