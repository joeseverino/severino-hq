"""Drop the machine fields nothing reads, and the one the tailnet reports.

Specs forbid extra keys, so each is removed from every spec here too.
Reversible: the fields are restored empty.
"""

from django.db import migrations

# Every field removed from MachineSpec. The tailnet reports `operating_system`;
# the other three are read only by the form's own readout.
FIELDS = ("operating_system", "form", "ssh_alias", "ssh_port")


def _strip(apps, schema_editor):
    ManagedResource = apps.get_model("control_plane", "ManagedResource")
    for resource in ManagedResource.objects.filter(kind="machine"):
        spec = dict(resource.spec or {})
        if not any(field in spec for field in FIELDS):
            continue
        for field in FIELDS:
            spec.pop(field, None)
        resource.spec = spec
        resource.save(update_fields=["spec"])


def _restore(apps, schema_editor):
    ManagedResource = apps.get_model("control_plane", "ManagedResource")
    for resource in ManagedResource.objects.filter(kind="machine"):
        spec = dict(resource.spec or {})
        for field in FIELDS:
            spec.setdefault(field, None if field == "ssh_port" else "")
        resource.spec = spec
        resource.save(update_fields=["spec"])


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0015_tailnet_device_keys_say_what_they_are"),
    ]

    operations = [migrations.RunPython(_strip, _restore)]
