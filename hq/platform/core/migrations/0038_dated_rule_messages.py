"""New messages for the dated rules on agent blocks and device approvals."""

from django.conf import settings
from django.db import migrations, models

import hq.platform.core.rules


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0037_audit_subject_and_cause"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterConstraint(
            model_name="agentdevice",
            name="agent_device_approval_is_dated",
            constraint=hq.platform.core.rules.Rule(
                condition=models.Q(("approved_by__isnull", True), ("approved_at__isnull", False), _connector="OR"),
                field="approved_at",
                name="agent_device_approval_is_dated",
                violation_error_message="An approval needs the time it was given.",
            ),
        ),
        migrations.AlterConstraint(
            model_name="agentdevice",
            name="agent_device_block_is_dated",
            constraint=hq.platform.core.rules.Rule(
                condition=models.Q(("blocked_by__isnull", True), ("blocked_at__isnull", False), _connector="OR"),
                field="blocked_at",
                name="agent_device_block_is_dated",
                violation_error_message="A block needs the time it was set.",
            ),
        ),
        migrations.AlterConstraint(
            model_name="agentidentity",
            name="agent_identity_block_is_dated",
            constraint=hq.platform.core.rules.Rule(
                condition=models.Q(("blocked_by__isnull", True), ("blocked_at__isnull", False), _connector="OR"),
                field="blocked_at",
                name="agent_identity_block_is_dated",
                violation_error_message="A block needs the time it was set.",
            ),
        ),
    ]
