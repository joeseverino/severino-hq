"""Give each ended run the outcome its result sentence is worded from (``run_outcome``).

An older run's result holds its worker's sentence and the reason it ended;
its outcome is read from those and from its tasks, as its worker now
reports one. A run that succeeded takes none of its sentence, which could
be its playbook's checks rather than what it did, so it says what its
readings or its tasks say.
"""

from django.db import migrations

# How an older run's reason reads as its ending, where its tasks do not say.
_ENDINGS = {
    "OutcomeUnknown": "unknown",
    "Cancelled": "cancelled",
    "Held": "paused",
    "LeaseLost": "lease_lost",
    "TimedOut": "timed_out",
}
_FAILED = {"failed": "task_failed", "unreachable": "unreachable"}


def _outcome(state: str, result: dict) -> dict:
    conditions = [item for item in result.get("conditions") or () if isinstance(item, dict)]
    reason = str(result.get("reason") or (conditions[0].get("reason") if conditions else "") or "")
    outcome = {"ending": "", "task": "", "host": "", "message": ""}
    if state == "succeeded":
        return {**outcome, "ending": "succeeded"}
    if reason == "SwitchedOff":
        return {**outcome, "ending": "cancelled", "message": "Switched off"}
    if reason in _ENDINGS:
        return {**outcome, "ending": _ENDINGS[reason]}
    failed = next((event for event in result.get("events") or () if event.get("outcome") in _FAILED), None)
    if failed is not None:
        ending = _FAILED[failed["outcome"]]
        task = str(failed.get("task") or "") if ending == "task_failed" else ""
        return {**outcome, "ending": ending, "task": task, "host": str(failed.get("host") or "")}
    message = str(result.get("message") or "")
    return {**outcome, "ending": "errored" if reason == "Failed" else "refused", "message": message}


def _hold_outcomes(apps, _schema_editor):
    OperationRequest = apps.get_model("control_plane", "OperationRequest")
    ended = OperationRequest.objects.filter(action="run", state__in=("succeeded", "failed", "cancelled"))
    for run in ended.exclude(result__has_key="outcome").iterator():
        result = dict(run.result or {})
        result["outcome"] = _outcome(run.state, result)
        OperationRequest.objects.filter(pk=run.pk).update(result=result)


class Migration(migrations.Migration):
    dependencies = [
        ("control_plane", "0080_run_events_list_their_warnings"),
    ]

    operations = [migrations.RunPython(_hold_outcomes, migrations.RunPython.noop)]
