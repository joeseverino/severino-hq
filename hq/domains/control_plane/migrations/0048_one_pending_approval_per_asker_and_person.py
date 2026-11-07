"""A pending ask is one per change and asker, the asker being a client as a
person: the person's key joins the uniqueness, so another person's identical
ask through the same client is their own."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("control_plane", "0047_capability_rule_subject_names_a_person")]

    operations = [
        migrations.RemoveConstraint(model_name="approvalrequest", name="one_pending_approval_per_requested_change"),
        migrations.AddConstraint(
            model_name="approvalrequest",
            constraint=models.UniqueConstraint(
                fields=(
                    "capability",
                    "target",
                    "content_fingerprint",
                    "requested_actor",
                    "requested_interface",
                    "requested_person_key",
                ),
                condition=models.Q(state="pending"),
                name="one_pending_approval_per_requested_change",
            ),
        ),
    ]
