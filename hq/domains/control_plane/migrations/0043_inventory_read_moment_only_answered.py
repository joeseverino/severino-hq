from django.db import migrations, models


def forget_unanswered(apps, _schema_editor):
    """Clear the moment of every row no read is known to have answered.

    An unreachable row with no records is either a failed first read, whose
    moment was no read, or an empty read since failed: which one is not on the
    row. None is the reading that claims nothing, and the next read that
    answers sets it.
    """

    inventory = apps.get_model("control_plane", "ProviderInventory")
    unanswered = [
        kind
        for kind, records in inventory.objects.filter(reachable=False).values_list("kind", "records")
        if not records
    ]
    inventory.objects.filter(kind__in=unanswered).update(observed_at=None)


def restore_moments(apps, _schema_editor):
    """Give every row a moment again, for the column that requires one: when it
    was last written."""

    inventory = apps.get_model("control_plane", "ProviderInventory")
    inventory.objects.filter(observed_at__isnull=True).update(observed_at=models.F("updated_at"))


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0042_reconcile_label"),
    ]

    operations = [
        migrations.AlterField(
            model_name="providerinventory",
            name="observed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(forget_unanswered, restore_moments),
    ]
