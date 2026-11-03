"""A failed job's stored error is a sentence HQ wrote, never a fault's own text.

A job a fault stopped, or one whose thread could not start, may hold the
fault's traceback or message as its error; each becomes the runner's sentence
for it. The traceback was never logged, so the sentence names the fault's kind
and claims no log line.
"""

import re

from django.db import migrations

# The marks of a stored traceback: its header, or a frame line where a long
# one was cut from the front.
TRACEBACK = "Traceback (most recent call last)"
FRAME = '\n  File "'
NOT_STARTED_BEFORE = "Could not start: "
NOT_STARTED = "HQ could not start it; its log holds why."
# A traceback's last line opens with the exception's dotted class name; only
# that name is kept, never the text after it.
KIND = re.compile(r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)(?::|$)")
UNKNOWN_KIND = "error"


def kind_of(traceback: str) -> str:
    lines = traceback.strip().splitlines()
    found = KIND.match(lines[-1]) if lines else None
    return found.group(1).rsplit(".", 1)[-1][:80] if found else UNKNOWN_KIND


def forward(apps, schema_editor):
    del schema_editor
    Job = apps.get_model("jobs", "Job")
    faulted = Job.objects.filter(error__contains=TRACEBACK) | Job.objects.filter(error__contains=FRAME)
    for job in faulted.only("pk", "error").iterator():
        Job.objects.filter(pk=job.pk).update(error=f"It stopped on an unexpected {kind_of(job.error)}.")
    Job.objects.filter(error__startswith=NOT_STARTED_BEFORE).update(error=NOT_STARTED)


class Migration(migrations.Migration):
    dependencies = [
        ("jobs", "0005_ids_are_time_ordered"),
    ]

    operations = [
        # The fault text is gone once rewritten, so there is nothing to put back.
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
