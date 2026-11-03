from django.db import migrations
from django.utils import timezone

RETIRED = "contact.submission.list"


def forget(apps, _schema_editor):
    """Drop every standing rule for the retired capability, and settle any call held under it.

    A held call could never be replayed, so it is stale. A settled approval is
    history and keeps the name it was decided under.
    """

    apps.get_model("control_plane", "CapabilityRule").objects.filter(capability=RETIRED).delete()
    apps.get_model("control_plane", "ApprovalRequest").objects.filter(capability=RETIRED, state="pending").update(
        state="stale", decided_at=timezone.now()
    )


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0039_capability_names_follow_the_pattern"),
    ]

    operations = [
        # Going back has nothing to restore: which rules named the capability is
        # gone with them, and an earlier release reads the missing ones as its default.
        migrations.RunPython(forget, migrations.RunPython.noop),
    ]
