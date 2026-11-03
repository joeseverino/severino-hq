"""The block's rule, on its own, so ``constraint_preflight`` counts the rows it would refuse."""

from django.db import migrations, models

import hq.platform.core.rules


class Migration(migrations.Migration):
    dependencies = [("core", "0019_agent_block")]

    operations = [
        migrations.AddConstraint(
            model_name="agentidentity",
            constraint=hq.platform.core.rules.Rule(
                condition=models.Q(("blocked_by__isnull", True), ("blocked_at__isnull", False), _connector="OR"),
                field="blocked_at",
                name="agent_identity_block_is_dated",
                violation_error_message="A block someone set says when they set it.",
            ),
        ),
    ]
