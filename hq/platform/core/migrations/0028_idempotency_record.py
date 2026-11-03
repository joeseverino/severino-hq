"""The retry-key record is core's, on the table hq_api made: state only, so the database is untouched."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0027_linked_account_login_is_one_persons"),
        ("hq_api", "0001_initial"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="IdempotencyRecord",
                    fields=[
                        (
                            "id",
                            models.BigAutoField(
                                auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                            ),
                        ),
                        ("actor", models.CharField(max_length=255)),
                        ("actor_sha256", models.CharField(max_length=64)),
                        ("key", models.CharField(max_length=128)),
                        ("request_sha256", models.CharField(max_length=64)),
                        ("response", models.JSONField(null=True)),
                        ("status_code", models.PositiveSmallIntegerField(null=True)),
                        ("created_at", models.DateTimeField(auto_now_add=True)),
                        ("expires_at", models.DateTimeField(db_index=True)),
                    ],
                    options={
                        "db_table": "hq_api_idempotencyrecord",
                        "ordering": ("-created_at",),
                        "constraints": [
                            models.UniqueConstraint(
                                fields=("actor_sha256", "key"),
                                name="unique_api_idempotency_key_per_actor",
                            )
                        ],
                    },
                ),
            ],
            database_operations=[],
        ),
    ]
