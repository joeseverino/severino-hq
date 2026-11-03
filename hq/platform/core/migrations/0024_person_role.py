"""The role each person holds in HQ."""

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

import hq.platform.core.rules


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0023_agent_signs_in_a_known_way"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PersonRole",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("role", models.CharField(choices=[("viewer", "Viewer"), ("operator", "Operator")], max_length=16)),
                ("resolved_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="person_role",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="personrole",
            constraint=hq.platform.core.rules.Rule(
                condition=models.Q(("role__in", ["viewer", "operator"])),
                field="role",
                name="person_role_is_a_known_role",
                violation_error_message="Must be one of: viewer, operator.",
            ),
        ),
    ]
