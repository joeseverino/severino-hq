"""An app update's way through its steps, as each seed leaves it: its
approval queued, refused, applied or held on its host, and the Open PR of an
app Renovate holds, from its press to the pull request its run opened."""

from datetime import timedelta

from django.utils import timezone

from hq.domains.control_plane.models import (
    InboundEvent,
    InboundOutcome,
    InboundSource,
    ManagedResource,
    ProviderInventory,
)
from hq.domains.control_plane.observations.github import DEPENDENCY_RUN_KIND, PROVIDER, PULL_REQUEST_KIND
from hq.domains.control_plane.provider_adapters import INBOUND, github as delivery

from .names import GITHUB, MACHINE, hexed, iso
from .sweep import store
from .work import APPS, HELD_APP, UPDATED_APP, app_update, check_run

# How the app update's approval ends in each seed that draws one: its preview
# waiting for the playbook worker, refused on the host, its merge made and the
# apply refused, held on the pull unit it started, or applied.
PREVIEW_QUEUED, PREVIEW_FAILED, NOT_APPLIED, HELD = "queued", "preview-failed", "not-applied", "held"
# The steps a run stops on when the host refuses to start the pull.
_REFUSED = (
    ("Ask sudo whether the deploy login may start the pull unit", "ok", ""),
    ("Require sudo to let the deploy login start the pull unit", "failed", "sudo refuses to start the pull unit."),
)


def _held(applied, merged: str) -> None:
    """``applied`` held on the pull unit its playbook started, a minute in,
    while the updated app's container is recreated on HQ's machine."""

    from hq.domains.control_plane.models import InboundEvent, InboundOutcome, InboundSource, OperationRequest
    from hq.domains.control_plane.observations.host import DOCKER_CONTAINER
    from hq.platform.application.resource_operations import HANDOFF

    now = timezone.now()
    claimed = now - timedelta(minutes=1)
    OperationRequest.objects.filter(pk=applied.pk).update(
        state=OperationRequest.State.CLAIMED,
        claimed_by="example-worker",
        claimed_at=claimed,
        completed_at=None,
        lease_expires_at=claimed + timedelta(seconds=1500),
        result={HANDOFF: {MACHINE: f"example-pull@{merged}.service"}},
    )
    for number, (event, ago) in enumerate((("destroy", 40), ("start", 35), ("health_status: healthy", 20)), start=1):
        InboundEvent.objects.create(
            provider="host",
            event=f"{DOCKER_CONTAINER}.{event}",
            subject=UPDATED_APP,
            delivery_id=f"00000000-0000-4000-9000-{number:012d}",
            source=InboundSource.HOST,
            received_at=now - timedelta(seconds=ago),
            applied_at=now - timedelta(seconds=ago),
            outcome=InboundOutcome.APPLIED,
        )


def approval(owner, ended: str) -> None:
    """The open app update approved and ended as ``ended`` says, each step
    queued as its approval queues it and ended as its worker leaves it."""

    from hq.domains.control_plane.models import OperationRequest
    from hq.domains.control_plane.provider_adapters.automation import OPERATION_KIND
    from hq.platform.application.automation_runs import CHECK
    from hq.platform.application.resource_operations import THEN, asked_fields, enqueue
    from hq.platform.application.security import web_principal

    update = app_update(APPS)
    repository = f"example/{APPS}"
    # A playbook with no run waiting, since a record queues one at a time.
    waiting = (OperationRequest.State.QUEUED, OperationRequest.State.CLAIMED)
    playbook = (
        ManagedResource.objects.filter(kind=OPERATION_KIND)
        .exclude(operations__state__in=waiting)
        .order_by("key")
        .first()
    )
    hosts = [MACHINE]
    applies = {
        "operation": playbook.spec["operation"],
        "revision": playbook.spec["revision"],
        "hosts": hosts,
        "correlation": f"{repository}#{update['number']}@{update['head'][:12]}",
    }
    events = [{"host": MACHINE, "task": task, "outcome": outcome, "message": said} for task, outcome, said in _REFUSED]
    at = timezone.now() - timedelta(minutes=5)

    def record(resource, action, state, *, asked, result):
        fields = asked_fields(web_principal(owner), reason="", **asked)
        operation, _ = enqueue(action, lambda: (resource, fields))
        finished = {"state": state, "result": result, "claimed_at": at, "completed_at": at}
        OperationRequest.objects.filter(pk=operation.pk).update(**finished)
        return operation

    merging = {"head_sha": update["head"], "apps": [UPDATED_APP], THEN: applies}
    previewed = {**applies, "parameters": {"revision": update["head"]}, CHECK: True, THEN: merging}
    if ended == PREVIEW_QUEUED:
        enqueue("run", lambda: (playbook, asked_fields(web_principal(owner), reason="", **previewed)))
        return
    if ended == PREVIEW_FAILED:
        record(playbook, "run", "failed", asked=previewed, result={"message": "A task failed.", "events": events})
        return
    pull = ManagedResource.objects.create(
        key=f"example-pull-request-{APPS}-{update['number']}",
        kind=delivery.PULL_REQUEST_KIND,
        spec={"repository": repository, "number": update["number"], "head_sha": update["head"]},
    )
    merged = hexed("merged", APPS, length=40)
    failed = ended == NOT_APPLIED
    applied = record(
        playbook,
        "run",
        "failed" if failed else "succeeded",
        asked={**applies, "parameters": {"revision": merged}},
        result={"message": "A task failed.", "events": events}
        if failed
        else {
            "stated": {
                "readings": {MACHINE: {"apps.receipt": [{"revision": merged, "last_attempt": merged, "ok": True}]}}
            }
        },
    )
    if ended == HELD:
        _held(applied, merged)
    record(
        pull,
        "merge",
        "succeeded",
        asked={**merging, "correlation": applies["correlation"]},
        result={"status": {"merge_sha": merged}, THEN: {"run": str(applied.pk)}},
    )


# How the Open PR of the app Renovate holds stands, by the seed that leaves it so.
ASKED, RUNNING, FAILED, OPENED = "asked", "running", "failed", "opened"
# What each run the seeds leave reported, after the press.
_RAN = {RUNNING: "handed_off", FAILED: "failed", OPENED: "ok"}


def opening(owner, at: str) -> None:
    """The app Renovate holds, its Open PR pressed and left as ``at`` says:
    its run asked, taken and running, failed, or ended having opened the
    app's pull request."""

    from hq.platform.application.capabilities import run_capability
    from hq.platform.application.read_requests import forced_reads, take
    from hq.platform.application.refusal_status import refused
    from hq.platform.application.security import web_principal

    payload = {"repository": f"example/{APPS}", "app": HELD_APP}
    answer = run_capability("app_update.open", payload, principal=web_principal(owner), idempotency_key="example-open")
    if refused(answer.answer):
        raise RuntimeError(f"The estate's Open PR was refused: {answer.answer}")
    if at == ASKED:
        return
    # The controller's pass took the ask and handed the run off.
    take(forced_reads())
    opened = {
        "repository": f"example/{APPS}",
        "branch": f"renovate/{HELD_APP}",
        "result": "pr-created",
        "pull_request": 61,
        "files": [f"compose/{HELD_APP}/compose.yaml"],
    }
    store(
        DEPENDENCY_RUN_KIND,
        {
            "connection_ref": GITHUB,
            "owner": "example",
            "repositories": [f"example/{APPS}"],
            "outcome": _RAN[at],
            "started_at": iso(),
            "finished_at": None if at == RUNNING else iso(),
            "branches": [opened] if at == OPENED else [],
        },
    )


def checks_running() -> None:
    """The open app update's head checked again: one check passed, one
    queued when it was read, and running since, as GitHub's job event told
    HQ after the read."""

    held = ProviderInventory.objects.get(kind=PULL_REQUEST_KIND).records
    queued = {**check_run("scan", "", timedelta(minutes=2)), "status": "queued", "started_at": None}
    running = {
        "checks": {"state": "pending", "total": 2, "failing": [], "running": 1, "names": ["validate", "scan"]},
        "check_runs": [check_run("validate", "success", timedelta(minutes=3), seconds=58), queued],
    }
    store(PULL_REQUEST_KIND, *({**each, **running} if each.get("number") == 58 else each for each in held))
    update = next(each for each in held if each.get("number") == 58)
    job = INBOUND[PROVIDER].facts["workflow_job"]
    said = job.read(job.example)
    started = iso(timedelta(minutes=1))
    InboundEvent.objects.create(
        provider=PROVIDER,
        event="workflow_job.in_progress",
        subject=str(update["repository"]),
        delivery_id=hexed("scan", length=36),
        source=InboundSource.WORKER,
        received_at=timezone.now(),
        outcome=InboundOutcome.APPLIED,
        about=str(update["head"]),
        said_at=timezone.now(),
        fact={**said["fact"], "name": "scan", "started_at": started, "url": queued["url"]},
    )
