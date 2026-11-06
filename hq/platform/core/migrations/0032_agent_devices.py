"""The tailnet devices an agent calls from, each approved or blocked on its own,
and the device an audit row names as a column with an index for a read by device.
"""

import django.db.models.deletion
import django.db.models.fields.json
import django.db.models.functions.comparison
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

import hq.platform.core.rules


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0031_idempotency_record_drop_status_code"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AgentDevice",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("node_id", models.CharField(max_length=64)),
                ("name", models.CharField(blank=True, max_length=200)),
                ("os", models.CharField(blank=True, max_length=64)),
                ("owner", models.CharField(blank=True, max_length=200)),
                ("first_seen", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("last_seen", models.DateTimeField(default=django.utils.timezone.now)),
                ("approved_at", models.DateTimeField(blank=True, null=True)),
                ("blocked_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "ordering": ("identity", "name", "node_id"),
            },
        ),
        migrations.AddField(
            model_name="auditlog",
            name="device",
            field=models.GeneratedField(
                db_persist=True,
                expression=django.db.models.functions.comparison.Coalesce(
                    django.db.models.fields.json.KeyTextTransform("device", "metadata"),
                    models.Value("", output_field=models.TextField()),
                ),
                output_field=models.CharField(max_length=64),
            ),
        ),
        migrations.AddIndex(
            model_name="auditlog",
            index=models.Index(fields=["device", "-created_at"], name="core_auditl_device_e335d4_idx"),
        ),
        migrations.AddField(
            model_name="agentdevice",
            name="approved_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="agentdevice",
            name="blocked_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="agentdevice",
            name="identity",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, related_name="devices", to="core.agentidentity"
            ),
        ),
        migrations.AddField(
            model_name="agentdevice",
            name="person",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddConstraint(
            model_name="agentdevice",
            constraint=models.UniqueConstraint(
                condition=models.Q(("person__isnull", False)),
                fields=("identity", "person", "node_id"),
                name="unique_agent_device_per_person",
            ),
        ),
        migrations.AddConstraint(
            model_name="agentdevice",
            constraint=models.UniqueConstraint(
                condition=models.Q(("person__isnull", True)),
                fields=("identity", "node_id"),
                name="unique_agent_device_per_machine",
            ),
        ),
        migrations.AddConstraint(
            model_name="agentdevice",
            constraint=hq.platform.core.rules.Rule(
                condition=models.Q(("approved_by__isnull", True), ("approved_at__isnull", False), _connector="OR"),
                field="approved_at",
                name="agent_device_approval_is_dated",
                violation_error_message="An approval someone gave says when they gave it.",
            ),
        ),
        migrations.AddConstraint(
            model_name="agentdevice",
            constraint=hq.platform.core.rules.Rule(
                condition=models.Q(("blocked_by__isnull", True), ("blocked_at__isnull", False), _connector="OR"),
                field="blocked_at",
                name="agent_device_block_is_dated",
                violation_error_message="A block someone set says when they set it.",
            ),
        ),
    ]
