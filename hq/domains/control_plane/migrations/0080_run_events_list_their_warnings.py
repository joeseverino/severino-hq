from django.db import migrations

# How a worker joined a task's warnings into one line before it sent each apart.
JOINED = "; "


def list_warnings(apps, _schema_editor):
    """Rewrite each stored run event's warnings, one line, as the list a worker sends."""

    OperationRequest = apps.get_model("control_plane", "OperationRequest")
    changed = []
    for operation in OperationRequest.objects.filter(result__has_key="events").only("pk", "result").iterator():
        events = operation.result.get("events") or []
        if not any(isinstance(event.get("warnings"), str) for event in events):
            continue
        for event in events:
            warned = event.get("warnings")
            if isinstance(warned, str):
                event["warnings"] = warned.split(JOINED) if warned else []
        changed.append(operation)
    OperationRequest.objects.bulk_update(changed, ["result"], batch_size=200)


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0079_inbound_events_hold_their_fact"),
    ]

    operations = [
        migrations.RunPython(list_warnings, migrations.RunPython.noop),
    ]
