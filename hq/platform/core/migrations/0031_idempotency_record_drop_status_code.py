from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0030_audit_done_action"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="idempotencyrecord",
            name="status_code",
        ),
    ]
