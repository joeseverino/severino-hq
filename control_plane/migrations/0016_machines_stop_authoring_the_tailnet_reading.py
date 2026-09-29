"""Drop the machine fields nothing reads, and the one the tailnet reports.

`operating_system` is a second place to write down something HQ reads from
the tailnet on every sweep. `form`, `ssh_alias` and `ssh_port` are read only by
the readout at the top of the form they are typed into: nothing resolves
through them and no page decides anything by them.

Specs validate with `extra="forbid"`, so a key left behind fails every machine
in the estate rather than being ignored. It is removed here rather than
tolerated, because a spec that validates only by accident is the next drift.

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
