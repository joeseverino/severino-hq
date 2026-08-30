"""Drop the machine fields nothing reads, and the one the tailnet reports.

`operating_system` was a second place to write down something HQ reads on
every sweep. It stayed blank on all seven machines while the tailnet panel on
the same page printed the answer, so the form implied HQ did not know beside
the fact that it did.

`form`, `ssh_alias` and `ssh_port` were blank on all seven too, and read by
exactly one thing: the readout at the top of the form they were typed into.
Nothing resolved through them and no page decided anything by them.

Specs validate with `extra="forbid"`, so a key left behind fails every machine
in the estate rather than being ignored. It is removed here rather than
tolerated, because a spec that validates only by accident is the next drift.

Reversible: the field is restored empty, which is what it held.
"""

from django.db import migrations


# Every field removed from MachineSpec in the same change. `operating_system`
# had a source all along -- the tailnet reports it -- and the other three were
# blank on every machine and read only by the readout at the top of the form
# they were typed into.
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
