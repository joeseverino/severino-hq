import hashlib
import json

from django.db import migrations, models


def pin_each_procedure(apps, schema_editor):
    """Each adopted procedure gains the commit its catalog was adopted at, and
    the files of that commit no procedure runs, which it may read; the digest
    is then over the procedures alone (``automation_adoption._digest``)."""

    catalogs = apps.get_model("control_plane", "AdoptedCatalog")
    for catalog in catalogs.objects.all():
        used = {path for procedure in catalog.procedures for path in procedure.get("files", {})}
        shared = {entry["path"]: entry["object"] for entry in catalog.files if entry["path"] not in used}
        procedures = [
            {**procedure, "revision": catalog.revision, "shared": dict(sorted(shared.items()))}
            for procedure in catalog.procedures
        ]
        material = json.dumps({"procedures": procedures}, sort_keys=True, separators=(",", ":"))
        catalog.procedures = procedures
        catalog.digest = hashlib.sha256(material.encode()).hexdigest()
        catalog.save(update_fields=("procedures", "digest"))


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0060_a_person_rejects_a_scan"),
    ]

    operations = [
        migrations.AddField(
            model_name="sourceproposal",
            name="rejected_playbooks",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.RunPython(pin_each_procedure, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="adoptedcatalog",
            name="files",
        ),
    ]
