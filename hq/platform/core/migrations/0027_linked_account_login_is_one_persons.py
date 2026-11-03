"""One account elsewhere is one person's, on its own, so ``constraint_preflight`` counts the rows it would refuse."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0026_agent_identity_person")]

    operations = [
        migrations.AddConstraint(
            model_name="linkedaccount",
            constraint=models.UniqueConstraint(fields=("provider", "login"), name="unique_linked_account_login"),
        ),
    ]
