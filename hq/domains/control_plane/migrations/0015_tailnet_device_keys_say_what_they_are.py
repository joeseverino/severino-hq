"""Rename tailnet device declarations to ``<name>-tailnet``, and the audit entries that name them.

A machine and its tailnet device both keyed on the bare name, so the second was
filed as ``<name>-2``. Audit entries name a resource by key so history survives
a deletion, so they are rewritten too. Reversible where the bare name is free.
"""

from django.db import migrations
from django.utils.text import slugify

TAILNET_KIND = "tailscale.device"
SUFFIX = "-tailnet"


def _keyed(hint: str) -> str:
    """The key ``suggest_key`` would produce for this hint, slugified the same way so the resource route matches it."""

    return slugify(hint.replace(".", "-"))[:180]


def _rename(apps, key_for):
    ManagedResource = apps.get_model("control_plane", "ManagedResource")
    AuditLog = apps.get_model("core", "AuditLog")

    taken = set(ManagedResource.objects.values_list("key", flat=True))
    for resource in ManagedResource.objects.filter(kind=TAILNET_KIND).order_by("key"):
        name = str((resource.spec or {}).get("name", "")).strip()
        if not name:
            continue
        wanted = key_for(name)
        if wanted == resource.key or wanted in taken:
            # Already right, or the target is occupied by something else. A
            # migration that renames onto an existing key would violate the
            # uniqueness it is trying to restore.
            continue
        old = resource.key
        taken.discard(old)
        taken.add(wanted)
        resource.key = wanted
        resource.save(update_fields=["key"])
        AuditLog.objects.filter(object_type="ManagedResource", object_id=old).update(object_id=wanted)


def forwards(apps, schema_editor):
    _rename(apps, lambda name: _keyed(f"{name}{SUFFIX}"))


def backwards(apps, schema_editor):
    _rename(apps, _keyed)


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0014_alter_operationrequest_action"),
        ("core", "0001_initial"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
