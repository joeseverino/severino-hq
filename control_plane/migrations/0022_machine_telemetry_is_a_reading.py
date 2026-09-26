"""Machine telemetry moves out of resource status into stored readings.

A queryset update, so no save signal fires and no audit row is written for
removing what should never have been audited.
"""

from django.db import migrations


def forward(apps, schema_editor):
    ManagedResource = apps.get_model("control_plane", "ManagedResource")
    UpstreamReading = apps.get_model("core", "UpstreamReading")
    for resource in ManagedResource.objects.filter(kind="machine", status__has_key="telemetry"):
        status = dict(resource.status)
        telemetry = status.pop("telemetry")
        if telemetry and resource.last_observed_at:
            UpstreamReading.objects.update_or_create(
                key=f"machine-telemetry:{resource.key}",
                defaults={"value": telemetry, "observed_at": resource.last_observed_at},
            )
        ManagedResource.objects.filter(pk=resource.pk).update(status=status)


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0021_capability_rule"),
        ("core", "0009_upstream_reading"),
    ]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
