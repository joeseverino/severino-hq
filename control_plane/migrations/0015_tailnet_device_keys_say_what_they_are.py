"""Rename tailnet device declarations to ``<name>-tailnet``.

A key is unique across every kind, and two providers asked for the same one: a
machine and its tailnet device both keyed on the bare device name. Whichever was
adopted second was filed as ``<name>-2`` -- a suffix recording nothing but
arrival order, on an estate where the name is what everything joins on. Read
back later it looks like a second machine, which is exactly how it was read.

``_tailnet_device_key_hint`` now qualifies the key, so nothing adopted after
this collides. This brings the declarations that already exist to the same
shape, because a rule that only applies to new records leaves the confusing ones
in place forever.

Audit entries are rewritten alongside. They reference a resource by key as a
string rather than by relation -- deliberately, so history survives the deletion
of the thing it describes -- which means a rename that ignored them would sever
every record of what was done to these devices. Operations follow their resource
by foreign key and need no help.

Reversible: the down migration restores the bare name where it is free, which is
the state this found.
"""

from __future__ import annotations

from django.db import migrations
from django.utils.text import slugify

TAILNET_KIND = "tailscale.device"
SUFFIX = "-tailnet"


def _keyed(hint: str) -> str:
    """The key ``suggest_key`` would produce for this hint.

    Slugified the same way and for the same reason: a device name is a display
    string -- "Joseph's MacBook Pro" -- and a key is an identifier that appears
    in a URL. Renaming without this turns a perfectly good key into one the
    resource route cannot match, which is a worse outcome than the suffix.

    Dots become separators first, exactly as ``suggest_key`` does, so a device
    named like a hostname does not slug into one unreadable word.
    """

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
        AuditLog.objects.filter(object_type="ManagedResource", object_id=old).update(
            object_id=wanted
        )


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
