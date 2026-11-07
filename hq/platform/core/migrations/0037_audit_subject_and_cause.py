"""The record an audit row is about and the operation that started its own, as
columns stored from its metadata, each with an index: a record's story and an
operation's chain are each one read.

The fields rebuild the table, which drops its append-only triggers;
``append_only.install`` puts them back after every migrate.
"""

import django.db.models.fields.json
import django.db.models.functions.comparison
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0036_audit_checked_label"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="auditlog",
            name="cause",
            field=models.GeneratedField(
                db_persist=True,
                expression=django.db.models.functions.comparison.Coalesce(
                    django.db.models.fields.json.KeyTextTransform("cause", "metadata"),
                    models.Value("", output_field=models.TextField()),
                ),
                output_field=models.CharField(max_length=128),
            ),
        ),
        migrations.AddField(
            model_name="auditlog",
            name="subject",
            field=models.GeneratedField(
                db_persist=True,
                expression=django.db.models.functions.comparison.Coalesce(
                    django.db.models.fields.json.KeyTextTransform("subject", "metadata"),
                    models.Value("", output_field=models.TextField()),
                ),
                output_field=models.CharField(max_length=200),
            ),
        ),
        migrations.AddIndex(
            model_name="auditlog",
            index=models.Index(fields=["subject", "created_at"], name="core_auditl_subject_c22361_idx"),
        ),
        migrations.AddIndex(
            model_name="auditlog",
            index=models.Index(fields=["cause"], name="core_auditl_cause_6c76fa_idx"),
        ),
    ]
