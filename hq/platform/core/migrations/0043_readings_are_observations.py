"""Machine figures, the forecast and address lookups are observation kinds
now, kept in the inventory. Their old rows are cached readings that are read
again when their page opens, so they are deleted rather than moved."""

from django.db import migrations
from django.db.models import Q


def forward(apps, schema_editor):
    apps.get_model("core", "UpstreamReading").objects.filter(
        Q(key__startswith="machine-telemetry:") | Q(key__startswith="weather:") | Q(key__startswith="public-address:")
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0042_audit_trace")]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
