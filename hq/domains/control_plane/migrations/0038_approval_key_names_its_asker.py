from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0037_sealed_secrets"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="approvalrequest",
            name="one_pending_approval_per_requested_change",
        ),
        migrations.AddConstraint(
            model_name="approvalrequest",
            constraint=models.UniqueConstraint(
                condition=models.Q(("state", "pending")),
                fields=("capability", "target", "content_fingerprint", "requested_actor", "requested_interface"),
                name="one_pending_approval_per_requested_change",
            ),
        ),
    ]
