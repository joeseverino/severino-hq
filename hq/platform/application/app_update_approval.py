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
from dataclasses import dataclass
from typing import Annotated, Any

from django.db import transaction
from django.utils.text import get_text_list
from pydantic import Field, ValidationError

from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.domains.control_plane.provider_adapters.github import COMMIT, PULL_REQUEST_KIND, REPOSITORY

from .command_fields import Describe
from .contracts import Effect
from .integration_specs import CapabilitySpec
from .record_errors import PolicyError, RecordNotFound, Sentence
from .record_fields import RecordId
from .security import Capability, PolicyDenied
from .tone import Tone
from .ui import counted, ended
lazy from .app_updates import AppUpdate, app_updates
lazy from .approval_review import ChangePreview
lazy from .approvals import ApprovalSubject, every_call, measured_by, waits_for_a_person
lazy from .automation_manifest import OperationManifest
lazy from .automation_runs import (
    AGAIN,
    CHECK,
    capability_name,
    operation_registry,
    queue_follow_up,
    queue_run,
    reached,
    resolved_hosts,
)
lazy from .capabilities import Consented, command_spec, consent
lazy from .capability_policy import Rule, decide
lazy from .container_findings import UPDATE
lazy from .entity_links import web_url
lazy from .finding_model import FindingRemedy
lazy from .infrastructure import ManagedResourceCommand, save_managed_resource, suggest_key
lazy from .pages import PageAction, command_reach
lazy from .payload_words import ChangeRow
lazy from .resource_operations import ACTION_CAPABILITY, THEN, OperationAsked, queue_carried_merge
lazy from .work_runs import run_url, stopped_at

# The input of the playbook that applies a merge: the commit it applies.
REVISION = "revision"
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
PLAYBOOKS = "Playbooks"


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
            f"No adopted playbook applies an app update. On {PLAYBOOKS}, Refresh and adopt the one that does."
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
    except PolicyError:
        then = None
    else:
        then = {"operation": manifest.id, "revision": manifest.revision, "hosts": list(manifest.hosts)}
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


def _shown(held: Any) -> ChangePreview:
    """Each pin it moves, and the files it changes one link away, on GitHub."""

    rows = tuple(
        ChangeRow(
            label=f"{pin.get('app', '')} {pin.get('service', '')}".strip(),
            before=str(pin.get("before") or ""),
            after=str(pin.get("after") or ""),
            change="changed" if pin.get("before") else "added",
        )
        for pin in held.baseline.get("pins") or ()
    )
    # Its subject is the pull request, `owner/name#number`, whose files GitHub shows under its address.
    pull = next(
        (
            web_url(update.url)
            for update in app_updates()
            if f"{update.repository}#{update.number}" == held.resource_key
        ),
        "",
    )
    return ChangePreview(
        label="Pins", rows=rows, lines=(), more_label=FILES if pull else "", more_url=f"{pull}/files" if pull else ""
    )


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
        raise PolicyError(ended(standing.words))
    hosts = resolved_hosts(manifest, manifest.hosts)
    if not hosts:
        raise PolicyError("The playbook that applies an app update names no machine to apply it on.")
    reached(manifest, hosts)
    pull = _declared(command, consented=consented)
    correlation = f"{command.repository}#{command.number}@{command.head_sha[:12]}"
    # The preview's record is the playbook's as approved (``queue_run``):
    # the apply runs only while it still is.
    return queue_run(
        manifest,
        {
            "operation": manifest.id,
            "revision": manifest.revision,
            "parameters": _parameters(manifest, command.head_sha),
            "hosts": list(hosts),
            "correlation": correlation,
            CHECK: True,
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
    )


def succeeded(operation: OperationRequest) -> None:
    """Queue the step ``operation`` carries now that it succeeded (a
    preview's merge, a merge's apply), and say on it what was queued or why
    nothing was."""

    then = (operation.input or {}).get(THEN)
    step = {OperationRequest.Action.RUN: _merge, OperationRequest.Action.MERGE: _follow}.get(operation.action)
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
            ACTION_CAPABILITY[OperationRequest.Action.MERGE],
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
                    "Queue a preview of that playbook at the head, in check mode, on its machines. Once it passes, "
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
    )


def _unapprovable(update: AppUpdate) -> Sentence:
    """Why ``update`` cannot be approved as it stands, or ""."""

    if not update.readable:
        return "HQ could not read the images this update moves from its Compose files. Review it on GitHub."
    if (update.checks or {}).get("state") != "success":
        return "Its checks are not passing. Approve it once they pass."
    return ""


# Why GitHub refused a merge (``github.MergeRefusal``), as the update says it.
REFUSED = {
    "head_moved": "GitHub refused the merge: the head moved after the approval.",
    "not_mergeable": "GitHub refused the merge: it cannot merge it as it stands.",
    "checks_failing": "GitHub refused the merge: its checks fail.",
    "not_installed": "GitHub refused the merge: HQ's GitHub App is not installed on its repository.",
}


@dataclass(frozen=True, slots=True)
class MergeStanding:
    """How an approved update stands: its preview, its merge, and the run
    that applies it."""

    words: str
    tone: str
    # The page of the run it stands on: the preview, or the apply.
    run_url: str = ""
    # Whether it previews, merges, applies or merged, so no second approval is offered.
    taken: bool = False
    # Of a preview or an apply that ended badly: the step it stopped on, and what it said.
    step: str = ""
    said: str = ""
    # The apply that failed, which Run again asks for again; "" otherwise.
    again: str = ""

    @property
    def lines(self) -> tuple[str, ...]:
        """Its fact's detail: the step it stopped on, then what it said."""

        return tuple(line for line in (self.step, self.said) if line)


def run_again_action(standing: MergeStanding) -> PageAction | None:
    """The press that asks for the failed apply again, its playbook's own
    form filled from that run (``finding_runs.run_again``); None while none failed."""

    run = OperationRequest.objects.filter(pk=standing.again).first() if standing.again else None
    if run is None:
        return None
    name = capability_name(str((run.input or {}).get("operation", "")))
    return PageAction.command(name, variant=AGAIN, query={"again": standing.again})


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
    url = run_url(str(preview.pk))
    if preview.state in _ACTIVE:
        return MergeStanding("Previewing", Tone.ATTENTION, url, taken=True)
    if preview.state == OperationRequest.State.SUCCEEDED:
        refused = str(((preview.result or {}).get(THEN) or {}).get("refused") or "")
        return MergeStanding(ended(f"Previewed. {refused}".strip()), Tone.SERIOUS, url)
    step, said = stopped_at(preview)
    return MergeStanding("Preview failed", Tone.SERIOUS, url, step=step, said=said)


def _merged(merge: OperationRequest) -> MergeStanding:
    result = merge.result or {}
    if merge.state in _ACTIVE:
        return MergeStanding("Merging", Tone.ATTENTION, taken=True)
    if merge.state == OperationRequest.State.SUCCEEDED:
        then = result.get(THEN) or {}
        if then.get("run"):
            return _applied(merge, str(then["run"]))
        return MergeStanding(ended(f"Merged. {then.get('refused') or ''}".strip()), Tone.SERIOUS, taken=True)
    refused = str((result.get("status") or {}).get("refused") or "")
    return MergeStanding(REFUSED.get(refused) or str(result.get("message") or "The merge failed."), Tone.SERIOUS)


def _applied(merge: OperationRequest, first: str) -> MergeStanding:
    """How the merge's apply stands: its newest run of the merge commit, a
    Run again's included, which acted rather than previewed."""

    queued = OperationRequest.objects.filter(pk=first).first()
    apply = (
        OperationRequest.objects.filter(
            action=OperationRequest.Action.RUN,
            resource_id=queued.resource_id,
            **{f"input__parameters__{REVISION}": (queued.input.get("parameters") or {}).get(REVISION)},
        )
        .exclude(input__has_key=CHECK)
        .order_by("-created_at")
        .first()
        if queued is not None
        else None
    )
    if apply is None:
        return MergeStanding("Merged", Tone.GOOD, run_url(first), taken=True)
    url = run_url(str(apply.pk))
    if apply.state in _ACTIVE:
        return MergeStanding("Merged, applying", Tone.ATTENTION, url, taken=True)
    if apply.state == OperationRequest.State.SUCCEEDED:
        return MergeStanding("Applied", Tone.GOOD, url, taken=True)
    step, said = stopped_at(apply)
    return MergeStanding(
        "Merged, not applied", Tone.SERIOUS, url, taken=True, step=step, said=said, again=str(apply.pk)
    )


def unapplied(repository: str, app: str) -> MergeStanding | None:
    """The newest approved merge of an update of ``app`` in ``repository``
    while it has no run that applied it; None once it applied or before one
    merged. Read where no update of the app is open any more."""

    merges = OperationRequest.objects.filter(
        action=OperationRequest.Action.MERGE,
        state=OperationRequest.State.SUCCEEDED,
        resource__kind=PULL_REQUEST_KIND,
        resource__spec__repository=repository,
    ).order_by("-created_at")
    merge = next((each for each in merges.iterator() if app in ((each.input or {}).get("apps") or ())), None)
    standing = _merged(merge) if merge is not None else None
    return standing if standing is not None and standing.tone != Tone.GOOD else None


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

    standing = merge_standing(update)
    if standing is not None and standing.taken:
        return Approval(standing, standing.words)
    try:
        manifest = applier()
    except PolicyError as refusal:
        return Approval(standing, str(refusal), adopt=True)
    if reason := _unapprovable(update):
        return Approval(standing, reason)
    hosts = resolved_hosts(manifest, manifest.hosts)
    where = f" on {get_text_list(list(hosts), 'and')}" if hosts else ""
    return Approval(standing, said=ended(f"Merges {update.label}, then applies it{where}"))


def approve_remedy(update: AppUpdate) -> FindingRemedy:
    """The press that approves ``update``, as a finding offers it while ``approval_of`` holds nothing back."""

    name = ACTION_CAPABILITY[OperationRequest.Action.MERGE]
    url, _ = command_reach(name, approve_target(update))
    return FindingRemedy(capability=name, target="", url=url, method="POST")
