"""Drop the machine field the tailnet already reports.

`operating_system` was a second place to write down something HQ reads on
every sweep. It stayed blank on all seven machines while the tailnet panel on
the same page printed the answer, so the form implied HQ did not know beside
the fact that it did.

Specs validate with `extra="forbid"`, so a key left behind fails every machine
in the estate rather than being ignored. It is removed here rather than
tolerated, because a spec that validates only by accident is the next drift.

Reversible: the field is restored empty, which is what it held.
"""

from django.db import migrations


FIELD = "operating_system"


def _strip(apps, schema_editor):
    ManagedResource = apps.get_model("control_plane", "ManagedResource")
    for resource in ManagedResource.objects.filter(kind="machine"):
        if FIELD not in (resource.spec or {}):
            continue
        spec = dict(resource.spec)
        spec.pop(FIELD, None)
        resource.spec = spec
        resource.save(update_fields=["spec"])


def _restore(apps, schema_editor):
    ManagedResource = apps.get_model("control_plane", "ManagedResource")
    for resource in ManagedResource.objects.filter(kind="machine"):
        spec = dict(resource.spec or {})
        spec.setdefault(FIELD, "")
        resource.spec = spec
        resource.save(update_fields=["spec"])


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0015_tailnet_device_keys_say_what_they_are"),
    ]

    operations = [migrations.RunPython(_strip, _restore)]
