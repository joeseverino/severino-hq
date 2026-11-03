"""The sign-in's rule, on its own, so ``constraint_preflight`` counts the rows it would refuse."""

from django.db import migrations, models

import hq.platform.core.rules


class Migration(migrations.Migration):
    dependencies = [("core", "0022_agent_sign_in")]

    operations = [
        migrations.AddConstraint(
            model_name="agentidentity",
            constraint=hq.platform.core.rules.Rule(
                condition=models.Q(("signs_in__in", ["", "machine", "person"])),
                field="signs_in",
                name="agent_identity_signs_in_a_known_way",
                violation_error_message="Must be blank or one of: machine, person.",
            ),
        ),
    ]
