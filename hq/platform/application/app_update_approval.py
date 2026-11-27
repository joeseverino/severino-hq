"""Approving an app update: one press previews the apply where the app runs,
merges the pull request at the head a person saw once the preview passes,
then applies the merge.

The preview and the apply are runs of the adopted playbook that fixes an
image update and takes the commit to apply as its ``revision``, the preview
in Ansible's check mode; the merge is the controller's
(``github.pull_request``'s ``merge``). Each step carries the next in its
input (``THEN``), queued once it succeeds (``succeeded``). What binds them
and what each may do: docs/Architecture.md, "Approving an app update".
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Annotated, Any, Final

from django.db import transaction
from django.utils import timezone
from django.utils.text import capfirst, get_text_list
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.domains.control_plane.provider_adapters.github import COMMIT, PULL_REQUEST_KIND, REPOSITORY
lazy from hq.domains.control_plane.names import normalized_hostname
lazy from hq.domains.control_plane.provider_adapters.declarations import MACHINE_KIND

from .app_updates import MergeStanding
from .command_fields import Describe
from .contracts import Effect
from .integration_specs import CapabilitySpec
from .record_errors import PolicyError, RecordNotFound, Sentence, worded
from .record_fields import RecordId
from .security import Capability, PolicyDenied
from .tone import Tone
from .ui import counted, ended
from .work_runs import StatedKind
lazy from .app_updates import AppUpdate, app_updates, merged_of
lazy from .approval_review import ChangePreview
lazy from .approvals import ApprovalSubject, every_call, measured_by, waits_for_a_person
lazy from .automation_manifest import REVISION_INPUT as REVISION, OperationManifest
lazy from .automation_runs import (
    CHECK,
    capability_name,
    operation_registry,
    queue_follow_up,
    queue_run,
    reached,
    run_in_way,
)
lazy from .cadence import controller_standing
lazy from .capabilities import Consented, command_spec, consent
lazy from .capability_policy import Rule, decide
lazy from .container_findings import UPDATE
lazy from .containers import Container
lazy from .entity_links import web_url
lazy from .github_estate import repositories
lazy from .images import ImageRef
lazy from .infrastructure import ManagedResourceCommand, save_managed_resource, suggest_key
lazy from .labels import dotted
lazy from .machines import served_by
lazy from .moments import when
lazy from .payload_words import ChangeRow
lazy from .resource_operations import ACTION_CAPABILITY, THEN, OperationAsked, queue_carried_merge
lazy from .work_runs import WorkRun, run_row, standing_of as run_standing

_COMMIT = re.compile(COMMIT)
_ACTIVE = (OperationRequest.State.QUEUED, OperationRequest.State.CLAIMED)


@dataclass(frozen=True, slots=True)
class ApproveUpdateCommand:
    """The update to approve, at the head commit you reviewed."""

    repository: Annotated[
        str, Field(pattern=REPOSITORY, max_length=140), Describe("The repository it is open in, as owner/name.")
    ]
    number: Annotated[RecordId, Describe("Its pull request's number.")]
    head_sha: Annotated[
        str,
        Field(pattern=COMMIT),
        Describe("The head commit you reviewed.", api="The update's `head`; a head that moved since is refused."),
    ]


# The page a playbook is adopted on, as its refusal names it.
PLAYBOOKS: Final = "Playbooks"
APPROVE = ACTION_CAPABILITY[OperationRequest.Action.MERGE]
APPLY = "app_update.apply"


def applier() -> OperationManifest:
    """The one adopted playbook that applies a merged app update: it fixes an
    image update and takes the commit to apply. Refused for none, and for
    more than one, which HQ never chooses between."""

    found = [
        manifest
        for manifest in operation_registry().values()
        if UPDATE in manifest.fixes and REVISION in manifest.input_type.model_fields
    ]
    if not found:
        raise PolicyError(
            f"No adopted playbook applies an app update. On {PLAYBOOKS}, adopt the one that does."
        )
    if len(found) > 1:
        raise PolicyError(
            f"{counted(len(found), 'adopted playbook applies', 'adopted playbooks apply')} an app update. Adopt only one."
        )
    return found[0]


def _updates(command: ApproveUpdateCommand) -> tuple[AppUpdate, ...]:
    """The open update of each app the pull request moves."""

    return tuple(
        update for update in app_updates() if (update.repository, update.number) == (command.repository, command.number)
    )


def _image(image: Any) -> str:
    return f"{image.name}:{image.tag}@{image.digest}" if image else ""


def _measured(command: ApproveUpdateCommand) -> ApprovalSubject | None:
    """What an approval is of: the head, every pin before and after, what the
    new pins fix and bring, and the run that applies the merge, so a moved
    head, a changed pin or a new adoption stales it."""

    updates = _updates(command)
    if not updates:
        return None
    try:
        manifest = applier()
        hosts = applied_on(manifest, tuple(item for update in updates for item in update.containers))
    except PolicyError:
        then = None
    else:
        then = {"operation": manifest.id, "revision": manifest.revision, "hosts": list(hosts)}
    return ApprovalSubject(
        PULL_REQUEST_KIND,
        f"{command.repository}#{command.number}",
        {
            "head": updates[0].head,
            "pins": [
                {
                    "app": update.app,
                    "service": pin.service,
                    "before": _image(pin.before),
                    "after": _image(pin.after),
                    "digest": pin.digest,
                }
                for update in updates
                for pin in update.pins
            ],
            "fixes": sorted({str(each.get("id", "")) for update in updates for each in update.fixes}),
            "brings": sorted({str(each.get("id", "")) for update in updates for each in update.brings}),
            THEN: then,
        },
    )


# The pull request's own view of every file it changes, as GitHub names its tab.
FILES = "Files changed"


# What an apply interrupts while a container restarts, by what the container
# does for each name (``machines.Serving``), in the words that say it.
_INTERRUPTS = {"serves": "serving", "resolves": "resolving", "routes": "routing"}


def interrupted(items: tuple[Container, ...]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """What applying ``items`` briefly interrupts as each restarts: the names
    each serves, resolves or routes, by that word (``_INTERRUPTS``)."""

    serving = served_by()
    found: dict[str, set[str]] = {}
    for item in items:
        held = serving.get((item.machine.name, item.running.name))
        for does, names in (("serves", held.names), *held.through) if held else ():
            found.setdefault(_INTERRUPTS[does], set()).update(names)
    return tuple((doing, tuple(sorted(names))) for doing, names in sorted(found.items()) if names)


def _shown(held: Any) -> ChangePreview:
    """Each pin it moves and what applying it interrupts, and the files it
    changes one link away, on GitHub."""

    pins = tuple(
        ChangeRow(
            label=f"{pin.get('app', '')} {pin.get('service', '')}".strip(),
            before=_release(pin.get("before")),
            after=_release(pin.get("after")),
            change="changed" if pin.get("before") else "added",
        )
        for pin in held.baseline.get("pins") or ()
    )
    # Its subject is the pull request, `owner/name#number`, whose files GitHub shows under its address.
    update = next((each for each in app_updates() if f"{each.repository}#{each.number}" == held.resource_key), None)
    interrupts = tuple(
        ChangeRow(label=f"Interrupts {doing}", before="", after=", ".join(names), change="")
        for doing, names in interrupted(update.containers if update else ())
    )
    pull = web_url(update.url) if update else ""
    return ChangePreview(
        label="Would change",
        rows=(*pins, *interrupts),
        lines=(),
        more_label=FILES if pull else "",
        more_url=f"{pull}/files" if pull else "",
    )


def _release(reference: Any) -> str:
    """A pin the consent holds whole, said as the release it names."""

    image = ImageRef.parse(reference)
    return image.said if image else ""


def _may_run(manifest: OperationManifest, principal: Any) -> None:
    """Refuse a principal the playbook's own run command would refuse: its
    authority, and an agent's rule that keeps it off that command."""

    spec = command_spec(capability_name(manifest.id))
    spec.authorize(principal)
    if decide(spec, principal, None, None).rule == Rule.DENY:
        raise PolicyDenied(f"{spec.name} is refused for {principal.actor}, so approving the update is too.")


def _declared(command: ApproveUpdateCommand, *, consented: Consented) -> ManagedResource:
    """The pull request's record, at the head approved."""

    spec = {"repository": command.repository, "number": command.number, "head_sha": command.head_sha}
    current = (
        ManagedResource.objects.select_for_update()
        .filter(kind=PULL_REQUEST_KIND, spec__repository=command.repository, spec__number=command.number)
        .first()
    )
    key = current.key if current else suggest_key(PULL_REQUEST_KIND, spec)
    save_managed_resource(
        ManagedResourceCommand(key=key, kind=PULL_REQUEST_KIND, spec=spec),
        consented=consented,
        current_key=key if current else None,
    )
    return ManagedResource.objects.select_for_update().get(key=key)


def _parameters(manifest: OperationManifest, revision: str) -> dict[str, Any]:
    try:
        return manifest.input_type.model_validate({REVISION: revision}).model_dump(mode="json")
    except ValidationError as refused:
        raise PolicyError("The playbook refused the commit as its revision, so nothing ran.") from refused


@transaction.atomic
def approve_update(command: ApproveUpdateCommand, *, consented: Consented) -> OperationAsked:
    """Preview the apply of the update at the head reviewed where it runs,
    carrying the merge to queue once the preview passes, and the run that
    applies the merge once it is made."""

    principal = consent(consented).principal
    manifest = applier()
    _may_run(manifest, principal)
    updates = _updates(command)
    if not updates:
        raise RecordNotFound("No open app update is that pull request.")
    if updates[0].head != command.head_sha:
        raise PolicyError("The update has a new head commit since you reviewed it. Review it again.")
    for update in updates:
        reason = _unapprovable(update)
        if reason:
            raise PolicyError(reason)
    standing = merge_standing(updates[0])
    if standing is not None and standing.taken:
        return standing.asked()
    hosts = applied_on(manifest, tuple(item for update in updates for item in update.containers))
    reached(manifest, hosts)
    pull = _declared(command, consented=consented)
    correlation = f"{command.repository}#{command.number}@{command.head_sha[:12]}"
    # The preview is the run in check mode, asked the one way every preview
    # is (``queue_run``), whose record is the playbook's as approved: the
    # apply runs only while it still is.
    return queue_run(
        manifest,
        {
            "operation": manifest.id,
            "revision": manifest.revision,
            "parameters": _parameters(manifest, command.head_sha),
            "hosts": list(hosts),
            "correlation": correlation,
            THEN: {
                "pull_request": pull.key,
                "head_sha": command.head_sha,
                "apps": sorted({update.app for update in updates}),
                THEN: {
                    "operation": manifest.id,
                    "revision": manifest.revision,
                    "hosts": list(hosts),
                    "correlation": correlation,
                },
            },
        },
        reason="",
        consented=consented,
        check=True,
    )


def applied_on(manifest: OperationManifest, items: tuple[Container, ...]) -> tuple[str, ...]:
    """The machines an update of ``items`` applies on: each one they run on,
    refused where the playbook may not run. The playbook releases every app
    of the machine it runs on and refuses a commit that moves an app the
    machine does not run, so it never runs anywhere else."""

    hosts = tuple(sorted({normalized_hostname(item.machine.name) for item in items}))
    if not hosts:
        raise PolicyError("HQ reads no container of the app, so it has no machine to apply it on.")
    if not all(manifest.admits(MACHINE_KIND, host) for host in hosts):
        raise PolicyError("The playbook that applies it does not run on every machine the app runs on.")
    return hosts


# The repository that holds an app's Compose files, and the app, as an
# update to apply or to open names them.
type Holder = Annotated[
    str, Field(pattern=REPOSITORY, max_length=140), Describe("The repository that holds it, as owner/name.")
]
type App = Annotated[
    str,
    Field(pattern=r"^[A-Za-z0-9_.-]+$", max_length=100),
    Describe("The app, as its directory of Compose files names it."),
]


@dataclass(frozen=True, slots=True)
class ApplyUpdateCommand:
    """The app to apply at the head of its repository's default branch you saw."""

    repository: Holder
    app: App
    head_sha: Annotated[
        str,
        Field(pattern=COMMIT),
        Describe(
            "The default branch's head you saw.", api="The repository's `head`; a head that moved since is refused."
        ),
    ]


@transaction.atomic
def apply_update(command: ApplyUpdateCommand, *, consented: Consented) -> OperationAsked:
    """Run the playbook that applies app updates at the default branch's head,
    where a container of the app still runs a pin from before it."""

    principal = consent(consented).principal
    manifest = applier()
    _may_run(manifest, principal)
    items = merged_of(command.repository, command.app)
    if not items:
        raise RecordNotFound("Every container of that app runs what its default branch pins.")
    if (repositories()[command.repository].head or {}).get("sha") != command.head_sha:
        raise PolicyError("Its default branch has a new head since you looked. Look again.")
    standing = _apply_standing(manifest.id, command.head_sha, items)
    if standing is not None and standing.taken:
        return standing.asked()
    hosts = applied_on(manifest, items)
    reached(manifest, hosts)
    return queue_run(
        manifest,
        {
            "operation": manifest.id,
            "revision": manifest.revision,
            "parameters": _parameters(manifest, command.head_sha),
            "hosts": list(hosts),
            "correlation": f"{command.repository}@{command.head_sha[:12]}",
        },
        reason="",
        consented=consented,
    )


def succeeded(operation: OperationRequest) -> None:
    """Queue the step ``operation`` carries now that it succeeded (a
    preview's merge, a merge's apply), and say on it what was queued or why
    nothing was."""

    then = (operation.input or {}).get(THEN)
    step = {
        OperationRequest.Action.RUN: _merge,
        OperationRequest.Action.MERGE: _follow,
    }.get(operation.action)
    if not isinstance(then, dict) or step is None:
        return
    try:
        with transaction.atomic():
            said = step(operation, then)
    except PolicyError as refused:
        said = {"refused": str(refused)}
    operation.result = {**(operation.result or {}), THEN: said}
    OperationRequest.objects.filter(pk=operation.pk).update(result=operation.result)


def _merge(preview: OperationRequest, then: dict[str, Any]) -> dict[str, str]:
    # Only a preview carries a merge: a run that acted merges nothing.
    if (preview.input or {}).get(CHECK) is not True:
        raise PolicyError("Only a preview queues a merge, so nothing merged.")
    pull = (
        ManagedResource.objects.select_for_update()
        .filter(kind=PULL_REQUEST_KIND, key=str(then.get("pull_request", "")))
        .first()
    )
    if pull is None or pull.spec.get("head_sha") != then.get("head_sha"):
        raise PolicyError("The pull request was approved at another head since, so nothing merged.")
    carries = {"head_sha": pull.spec["head_sha"], "correlation": str(preview.input.get("correlation", ""))}
    merge = queue_carried_merge(
        pull, asked=preview, carries={**carries, "apps": then.get("apps"), THEN: then.get(THEN)}
    )
    return {"merge": str(merge.pk)}


def _follow(merge: OperationRequest, then: dict[str, Any]) -> dict[str, str]:
    # GitHub's sha is the one value the approval did not fix.
    sha = str(((merge.result or {}).get("status") or {}).get("merge_sha") or "")
    if not _COMMIT.fullmatch(sha):
        raise PolicyError("GitHub reported no commit for the merge, so nothing was applied.")
    manifest = operation_registry().get(str(then.get("operation", "")))
    if manifest is None or manifest.revision != then.get("revision"):
        raise PolicyError("The playbook adopted now is not the one approved, so nothing ran.")
    hosts = [str(host) for host in then.get("hosts") or ()]
    reached(manifest, tuple(hosts))
    bound = {
        "operation": manifest.id,
        "revision": manifest.revision,
        "parameters": _parameters(manifest, sha),
        "hosts": hosts,
        "correlation": str(then.get("correlation", "")),
    }
    return {"run": str(queue_follow_up(manifest, bound, asked=merge, once=f"{manifest.id}:{sha}").pk)}


def capabilities() -> tuple[CapabilitySpec, ...]:
    handler = waits_for_a_person(every_call)(measured_by(_measured, shown=_shown)(approve_update))
    return (
        CapabilitySpec(
            APPROVE,
            "Approve an app update: preview applying it where the app runs, then merge its pull request at the "
            "head you reviewed and apply the merge.",
            Effect.INFRASTRUCTURE_CHANGE,
            Capability.MANAGE_INFRASTRUCTURE,
            ApproveUpdateCommand,
            handler,
            execution_notes=(
                "Refuse unless one adopted playbook applies app updates and you may run it.",
                "Refuse an update whose head moved, whose diff GitHub did not give, or whose checks are not passing.",
                "Refuse while an approval at that head previews, merges or applies, or once it merged.",
                (
                    "Queue a preview of that playbook at the head, in check mode, on the machines the app runs on. "
                    "Once it passes, "
                    "queue the merge at that head; once GitHub makes the merge commit, queue one run of the "
                    "playbook with the commit as its revision. Each runs as you, joined by one correlation."
                ),
                "A preview that fails merges nothing, and the update says which step it stopped on.",
                "When an agent asks, it waits for an operator's approval.",
            ),
            label="Approve update",
            verb="Approve",
            wants="merge and apply an app update",
        ),
        CapabilitySpec(
            APPLY,
            "Apply the release an app's default branch pins where it runs something older.",
            Effect.INFRASTRUCTURE_CHANGE,
            Capability.MANAGE_INFRASTRUCTURE,
            ApplyUpdateCommand,
            waits_for_a_person(every_call)(apply_update),
            execution_notes=(
                "Refuse unless one adopted playbook applies app updates and you may run it.",
                "Refuse an app whose containers each run what the default branch pins, or a head that moved.",
                "Refuse while a run of that head waits or works, or once one applied it, until the app is read again.",
                "Refuse where a container of the app runs on a machine the playbook does not run on.",
                "Queue one run of the playbook with the head as its revision, as you.",
                "When an agent asks, it waits for an operator's approval.",
            ),
            label="Apply update",
            verb="Apply",
            wants="apply an app update",
        ),
    )


def _unapprovable(update: AppUpdate) -> Sentence:
    """Why ``update`` cannot be approved as it stands, or ""."""

    if not update.renovates:
        return worded("HQ's Renovate run did not open it from this repository. Review it on GitHub.")
    if not update.whole:
        return worded("HQ could not read every file it changes. Review it on GitHub.")
    if not update.within_apps:
        return worded("It changes files outside the apps it names. Review it on GitHub.")
    if not update.readable:
        return worded("HQ could not read the images this update moves from its Compose files. Review it on GitHub.")
    if (checks := (update.checks or {}).get("state")) == "pending":
        return worded("Its checks are running. Approve it once they pass.")
    if checks != "success":
        return worded("Its checks are not passing. Approve it once they pass.")
    return worded("")


# Why GitHub refused a merge (``github.MergeRefusal``), as the update says it.
REFUSED = {
    "head_moved": ("Merge refused", "GitHub's answer: the head moved after the approval"),
    "not_mergeable": ("Merge refused", "GitHub's answer: it cannot merge it as it stands"),
    "checks_failing": ("Merge refused", "GitHub's answer: its checks fail"),
    "checks_pending": ("Merge held", "Its checks have not all passed"),
    "not_installed": ("Merge refused", "HQ's GitHub App is not installed on its repository"),
}


def merge_standing(update: AppUpdate) -> MergeStanding | None:
    """Where the approval of ``update`` at its head stands, as the update says
    it: its newest preview or merge; None before either is asked."""

    merge = (
        OperationRequest.objects.filter(
            action=OperationRequest.Action.MERGE,
            resource__kind=PULL_REQUEST_KIND,
            resource__spec__repository=update.repository,
            resource__spec__number=update.number,
            input__head_sha=update.head,
        )
        .order_by("-created_at")
        .first()
    )
    preview = (
        OperationRequest.objects.filter(
            action=OperationRequest.Action.RUN,
            input__check=True,
            input__then__head_sha=update.head,
            input__correlation__startswith=f"{update.repository}#{update.number}@",
        )
        .order_by("-created_at")
        .first()
    )
    if preview is not None and (merge is None or preview.created_at > merge.created_at):
        return _previewed(preview)
    return _merged(merge) if merge is not None else None


def _previewed(preview: OperationRequest) -> MergeStanding:
    run = run_row(preview)
    if preview.state in _ACTIVE:
        return _under_way(preview, run)
    if preview.state == OperationRequest.State.SUCCEEDED:
        refused = str(((preview.result or {}).get(THEN) or {}).get("refused") or "")
        return MergeStanding("Previewed", Tone.SERIOUS, lines=(refused or "Not merged",), operation=preview, run=run)
    return _failed("Preview failed", "approve", preview, run)


def _merged(merge: OperationRequest) -> MergeStanding:
    result = merge.result or {}
    if merge.state in _ACTIVE:
        return _under_way(merge, run_row(merge))
    if merge.state == OperationRequest.State.SUCCEEDED:
        then = result.get(THEN) or {}
        if then.get("run"):
            return _applied(merge, str(then["run"]))
        return MergeStanding(
            "Not applied",
            Tone.SERIOUS,
            taken=True,
            merged=True,
            lines=(str(then.get("refused") or "Its apply was not asked"),),
            operation=merge,
        )
    refused = str((result.get("status") or {}).get("refused") or "")
    word, why = REFUSED.get(refused) or ("Merge failed", str(result.get("message") or ""))
    return MergeStanding(word, Tone.SERIOUS, lines=(why or "GitHub gave no reason",), operation=merge)


def _applied(merge: OperationRequest, first: str) -> MergeStanding:
    """How the merge's apply stands: its newest run of the merge commit."""

    queued = OperationRequest.objects.filter(pk=first).first()
    if queued is None:
        return MergeStanding("Merged", Tone.GOOD, taken=True, merged=True, operation=merge)
    revision = str(((queued.input or {}).get("parameters") or {}).get(REVISION) or "")
    standing = _apply_standing(str((queued.input or {}).get("operation", "")), revision) or MergeStanding(
        "Merged", Tone.GOOD, taken=True, operation=queued, run=run_row(queued)
    )
    # Once merged, a failed apply is pressed again from the default branch (``app_updates.UpdateKind.MERGED``).
    return replace(standing, taken=True, merged=True)


def _apply_standing(operation: str, revision: str, items: tuple[Container, ...] = ()) -> MergeStanding | None:
    """How the newest run of the playbook ``operation`` that applied (rather
    than previewed) the commit ``revision`` stands; None before one is asked,
    and once ``items``, which run a pin from before it, were read after it
    applied, since what they run says it no longer holds."""

    apply = (
        OperationRequest.objects.filter(
            action=OperationRequest.Action.RUN,
            input__operation=operation,
            **{f"input__parameters__{REVISION}": revision},
        )
        .exclude(input__has_key=CHECK)
        .order_by("-created_at")
        .first()
    )
    if apply is None:
        return None
    run = run_row(apply)
    if apply.state in _ACTIVE:
        return _under_way(apply, run)
    if apply.state == OperationRequest.State.SUCCEEDED:
        done = apply.completed_at
        if done is not None and any(item.running.observed_at > done for item in items):
            return None
        # Applied only where every machine's pull released every app.
        if pull_applied(run.evidence.stated):
            return MergeStanding("Applied", Tone.GOOD, taken=True, operation=apply, run=run)
        return _failed("Incomplete", "apply", apply, run)
    return _failed("Apply failed", "apply", apply, run)


def _under_way(operation: OperationRequest, run: WorkRun) -> MergeStanding:
    """A step under way, said as its run stands (``work_runs.standing_of``),
    with what it waits on while nothing has taken it."""

    standing = run_standing(run)
    return MergeStanding(
        standing.label,
        Tone.ATTENTION,
        taken=True,
        lines=(
            standing.note,
            waits_on(operation),
            f"asked {when(standing.since)}" if standing.since else "",
        ),
        operation=operation,
        run=run,
    )


def _failed(what: str, again: str, operation: OperationRequest, run: WorkRun) -> MergeStanding:
    """A step that ended badly, with the task it stopped on and the press
    that asks it again; its detail what that task said and each app the run
    did not release."""

    step, said = run.stopped
    return MergeStanding(
        what,
        Tone.SERIOUS,
        lines=(
            f"At “{step}”" if step else "",
            said,
            *(line for line, _tone in run.evidence.amiss),
            f"Fix what stopped it, then {again} again",
        ),
        operation=operation,
        run=run,
    )


def waits_on(operation: OperationRequest) -> str:
    """What a queued step the controller takes waits for while it does not
    report, "" once it does; a playbook's run says what it waits on as it
    stands (``work_runs.standing_of``)."""

    if operation.state != OperationRequest.State.QUEUED or operation.action == OperationRequest.Action.RUN:
        return ""
    return controller_standing().cannot("", now=timezone.now())


# The readings the applier states of each machine it runs on, each record
# read through its model alone: what each app runs after the pull, stated
# once the machine's last pull is of the revision asked, and one record of
# that pull, stated by a preview too.
PULLED = "apps.pull"
RECEIPT = "apps.receipt"
RELEASED = "ok"


class AppRelease(BaseModel):
    """What the applier did with one service of an app
    (``compose_project_outcomes``): ``result`` one of ``_RESULTS``,
    ``changed`` whether it replaced what ran, ``image`` what runs after."""

    model_config = ConfigDict(frozen=True)

    project: str
    service: str = ""
    image: str = ""
    result: str
    health: str = ""
    changed: bool


class PullReceipt(BaseModel):
    """The machine's last pull, beside the revision a run asked for: the
    revision that pull attempted, and whether it was of the one asked and
    released every app."""

    model_config = ConfigDict(frozen=True)

    revision: str
    last_attempt: str
    ok: bool


def _read[Model: BaseModel](model: type[Model], record: Mapping[str, Any]) -> Model | None:
    try:
        return model.model_validate(record)
    except ValidationError:
        return None


# Each result in its words and the tone a record of it is said in; one that
# released an app it did not change is said as left alone.
_RESULTS = {
    RELEASED: ("Released", Tone.GOOD),
    "refused": ("Refused; its trial failed, so it runs as before", Tone.ATTENTION),
    "rolled_back": ("Rolled back", Tone.ATTENTION),
    "failed": ("Failed", Tone.SERIOUS),
}
UNCHANGED = ("Unchanged", Tone.NEUTRAL)


def _result(result: str, *, changed: bool = True) -> tuple[str, Tone]:
    if result == RELEASED and not changed:
        return UNCHANGED
    return _RESULTS.get(result, (capfirst(result), Tone.NEUTRAL))


def _released(record: Mapping[str, Any]) -> tuple[str, Tone]:
    """What the applier did with one app and its tone; unread for a record not of its shape."""

    release = _read(AppRelease, record)
    return _result(release.result, changed=release.changed) if release else ("Unread", Tone.NEUTRAL)


def _pulled(record: Mapping[str, Any]) -> str:
    """What the applier did with one service, the release it runs and the health it found."""

    release = _read(AppRelease, record)
    if release is None:
        return _released(record)[0]
    image = ImageRef.parse(release.image)
    return dotted(_released(record)[0], image.said if image else "", release.health)


def _release_title(record: Mapping[str, Any]) -> str:
    """The app, and its service where the service has a name of its own."""

    project, service = str(record.get("project") or ""), str(record.get("service") or "")
    return dotted(project, service) if service not in ("", project) else project


PULLED_KIND = StatedKind(
    "App releases",
    title=_release_title,
    describe=_pulled,
    tone=lambda record: _released(record)[1],
)


def _receipt_said(record: Mapping[str, Any]) -> tuple[str, Tone]:
    """The machine's last pull as it stands beside the revision asked: only
    one of that revision that released every app applied it."""

    receipt = _read(PullReceipt, record)
    if receipt is None:
        return "Unread", Tone.NEUTRAL
    if receipt.ok:
        return "Applied", Tone.GOOD
    if receipt.last_attempt == receipt.revision:
        return "Not every app released", Tone.SERIOUS
    return (f"Last attempted {receipt.last_attempt[:12]}" if receipt.last_attempt else "Never pulled"), Tone.NEUTRAL


RECEIPT_KIND = StatedKind(
    "Pull",
    title=lambda record: str(record.get("revision") or "")[:12],
    describe=lambda record: _receipt_said(record)[0],
    tone=lambda record: _receipt_said(record)[1],
)


def pull_applied(stated: tuple[dict[str, Any], ...]) -> bool:
    """Whether a run's readings say every machine's pull applied the revision it asked."""

    receipts = [_read(PullReceipt, record) for each in stated if each["kind"] == RECEIPT for record in each["records"]]
    return bool(receipts) and all(receipt is not None and receipt.ok for receipt in receipts)


def applied_at(machine: str, app: str) -> str:
    """The revision ``machine`` last released ``app`` at: that of the newest
    run that succeeded there whose ``apps.pull`` reading has the app
    released, or "" where none has."""

    host = normalized_hostname(machine)
    runs = OperationRequest.objects.filter(
        action=OperationRequest.Action.RUN,
        state=OperationRequest.State.SUCCEEDED,
        **{f"result__stated__readings__{host}__has_key": PULLED},
    ).order_by("-created_at")
    for run in runs.only("input", "result").iterator():
        pulled = (_read(AppRelease, each) for each in run.result["stated"]["readings"][host][PULLED] or ())
        if any(each is not None and (each.project, each.result) == (app, RELEASED) for each in pulled):
            return str(((run.input or {}).get("parameters") or {}).get(REVISION) or "")
    return ""


def apply_target(managed: Any, head: str) -> dict[str, str]:
    """The route arguments a press applying ``managed``'s app at ``head`` posts to."""

    owner, _, name = managed.repository.partition("/")
    return {"owner": owner, "name": name, "app": managed.app, "head_sha": head}


def approve_target(update: AppUpdate) -> dict[str, str]:
    """The route arguments a press approving ``update`` at its head posts to."""

    owner, _, name = update.repository.partition("/")
    return {"owner": owner, "name": name, "number": str(update.number), "head_sha": update.head}


@dataclass(frozen=True, slots=True)
class Approval:
    """Where approving an update stands, as its page, its finding and its
    record each show it: the merge asked at its head, why no approval is
    offered (``held``, "" when one is), and what the press does when it is."""

    standing: MergeStanding | None
    held: str = ""
    said: str = ""
    # Held because no one adopted playbook applies it: the Playbooks page's step.
    adopt: bool = False


def approval_of(update: AppUpdate) -> Approval:
    """How approving ``update`` stands now. The applier is resolved as the
    approval resolves it (``applier``), so no surface offers a press it refuses."""

    return _approval(
        merge_standing(update),
        update.containers,
        _unapprovable(update),
        f"Merges {update.label} and applies its merge commit",
    )


def apply_of(repository: str, head: str, items: tuple[Container, ...]) -> Approval:
    """How applying ``repository``'s default branch at ``head`` where ``items`` run stands now."""

    try:
        manifest = applier()
    except PolicyError as refusal:
        return Approval(None, str(refusal), adopt=True)
    return _approval(_apply_standing(manifest.id, head, items), items, "", f"Applies {repository} at {head[:7]}")


def _approval(standing: MergeStanding | None, items: tuple[Container, ...], refused: str, does: str) -> Approval:
    """Its press held back while a step of it stands taken, no one playbook
    applies it, it is ``refused``, or that playbook does not run where ``items`` run."""

    if standing is not None and standing.taken:
        return Approval(standing, standing.words)
    try:
        manifest = applier()
    except PolicyError as refusal:
        return Approval(standing, str(refusal), adopt=True)
    if refused:
        return Approval(standing, refused)
    try:
        hosts = applied_on(manifest, items)
    except PolicyError as refusal:
        return Approval(standing, str(refusal))
    # A press while another run holds the playbook or its machines is refused (``queue_run``).
    if busy := run_in_way(manifest, hosts):
        return Approval(standing, busy)
    interrupts = [f"{doing} {counted(len(names), 'name')}" for doing, names in interrupted(items)]
    said = ended(f"{does} on {get_text_list(list(hosts), 'and')}")
    return Approval(
        standing, said=ended(f"{said} Interrupts {get_text_list(interrupts, 'and')}") if interrupts else said
    )
