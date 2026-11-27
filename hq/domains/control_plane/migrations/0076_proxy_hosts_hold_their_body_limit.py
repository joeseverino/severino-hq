"""Move a proxy host's body limit out of its advanced config into its own field.

The spec refuses ``client_max_body_size`` in the advanced config, since the
controller now writes the field as that directive. The comment lines just
above a moved directive say what it was for, so they go with it.
"""

import math
import re

from django.db import migrations

KIND = "npm.proxy_host"
DIRECTIVE = re.compile(r"^\s*client_max_body_size\s+(\d+)([kmg]?)\s*;\s*$", re.IGNORECASE)
# Bytes in each of nginx's units.
UNITS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def _moved(advanced: str) -> tuple[int, str] | None:
    lines = advanced.split("\n")
    at = next((index for index, line in enumerate(lines) if DIRECTIVE.match(line)), None)
    if at is None:
        return None
    found = DIRECTIVE.match(lines[at])
    size, unit = int(found[1]), found[2].lower()
    start = at
    while start > 0 and lines[start - 1].lstrip().startswith("#"):
        start -= 1
    kept = "\n".join(lines[:start] + lines[at + 1 :]).strip("\n")
    return math.ceil(size * UNITS[unit] / 1024**2), kept


def _move(apps, schema_editor):
    ManagedResource = apps.get_model("control_plane", "ManagedResource")
    for resource in ManagedResource.objects.filter(kind=KIND):
        spec = dict(resource.spec or {})
        moved = _moved(str(spec.get("advanced_config") or ""))
        if moved is None:
            continue
        spec["client_max_body_size"], spec["advanced_config"] = moved
        resource.spec = spec
        resource.save(update_fields=["spec"])


def _restore(apps, schema_editor):
    ManagedResource = apps.get_model("control_plane", "ManagedResource")
    for resource in ManagedResource.objects.filter(kind=KIND):
        spec = dict(resource.spec or {})
        limit = spec.pop("client_max_body_size", 0)
        if limit:
            advanced = str(spec.get("advanced_config") or "")
            spec["advanced_config"] = "\n".join(filter(None, (f"client_max_body_size {limit}m;", advanced)))
        resource.spec = spec
        resource.save(update_fields=["spec"])


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0075_digests_name_their_main_modules"),
    ]

    operations = [migrations.RunPython(_move, _restore)]
