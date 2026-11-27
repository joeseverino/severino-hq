"""Move a proxy host's body limit out of its advanced config into its own field.

The spec refuses ``client_max_body_size`` in the advanced config, since the
controller now writes the field as that directive. The comment lines just
above a moved directive say what it was for, so they go with it.
"""

from django.db import migrations

from hq.domains.control_plane.provider_adapters.npm import body_limit

KIND = "npm.proxy_host"


def _move(apps, schema_editor):
    ManagedResource = apps.get_model("control_plane", "ManagedResource")
    for resource in ManagedResource.objects.filter(kind=KIND):
        spec = dict(resource.spec or {})
        moved = body_limit(str(spec.get("advanced_config") or ""))
        if moved is None:
            continue
        spec["client_max_body_size"], spec["advanced_config"] = moved
        resource.spec = spec
        resource.save(update_fields=["spec"])


def _restore(apps, schema_editor):
    ManagedResource = apps.get_model("control_plane", "ManagedResource")
    for resource in ManagedResource.objects.filter(kind=KIND):
        spec = dict(resource.spec or {})
        limit = spec.pop("client_max_body_size", None)
        if limit is not None:
            advanced = str(spec.get("advanced_config") or "")
            spec["advanced_config"] = "\n".join(filter(None, (f"client_max_body_size {limit}m;", advanced)))
        resource.spec = spec
        resource.save(update_fields=["spec"])


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0075_digests_name_their_main_modules"),
    ]

    operations = [migrations.RunPython(_move, _restore)]
