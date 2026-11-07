"""Who acted on an audit row, as columns: ``actor`` and ``interface``, stored
from its metadata, with an index for a read by actor.

A row a signed-in user wrote with no operation around it names them as its
actor, so it still says who acted once the user is deleted. The backfill runs
after the fields, whose rebuilds drop the table's triggers before its UPDATE.
"""

import django.db.models.fields.json
import django.db.models.functions.comparison
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import migrations, models


def _name_the_user(apps, schema_editor):
    audit = apps.get_model("core", "AuditLog")
    users = apps.get_model(settings.AUTH_USER_MODEL)
    quote = schema_editor.quote_name
    username = users._meta.get_field(get_user_model().USERNAME_FIELD).column
    schema_editor.execute(
        f"UPDATE {quote(audit._meta.db_table)} "  # noqa: S608 - models' own identifiers, quoted
        f"SET metadata = json_set(metadata, '$.actor', "
        f"(SELECT {quote(username)} FROM {quote(users._meta.db_table)} WHERE id = user_id)) "
        "WHERE user_id IS NOT NULL AND json_type(metadata) = 'object' "
        "AND json_type(metadata, '$.actor') IS NULL"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0028_idempotency_record"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="auditlog",
            name="actor",
            field=models.GeneratedField(
                db_persist=True,
                expression=django.db.models.functions.comparison.Coalesce(
                    django.db.models.fields.json.KeyTextTransform("actor", "metadata"),
                    models.Value("", output_field=models.TextField()),
                ),
                output_field=models.CharField(max_length=200),
            ),
        ),
        migrations.AddField(
            model_name="auditlog",
            name="interface",
            field=models.GeneratedField(
                db_persist=True,
                expression=django.db.models.functions.comparison.Coalesce(
                    django.db.models.fields.json.KeyTextTransform("interface", "metadata"),
                    models.Value("", output_field=models.TextField()),
                ),
                output_field=models.CharField(max_length=32),
            ),
        ),
        migrations.AddIndex(
            model_name="auditlog",
            index=models.Index(fields=["actor", "-created_at"], name="core_auditl_actor_1d8ea9_idx"),
        ),
        migrations.RunPython(_name_the_user, migrations.RunPython.noop),
    ]
