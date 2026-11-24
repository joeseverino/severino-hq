"""Approving an app update: one press merges its pull request at the head a
person saw, then applies the merge where the app runs.

The merge is the controller's (``github.pull_request``'s ``merge``), and the
apply is a run of the adopted playbook that fixes an image update and takes
the commit to apply as its ``revision``. What binds the two and what each
may do: docs/Architecture.md, "Approving an app update".
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
from .record_errors import PolicyError, RecordNotFound
from .record_fields import RecordId
from .security import Capability, PolicyDenied
lazy from .app_updates import AppUpdate, app_updates
lazy from .approval_review import ChangePreview
lazy from .approvals import ApprovalSubject, every_call, measured_by, waits_for_a_person
lazy from .automation_manifest import OperationManifest
lazy from .automation_runs import (
    capability_name,
    operation_registry,
    queue_follow_up,
    reached,
    record_of,
    resolved_hosts,
)
lazy from .capabilities import Consented, command_spec, consent
lazy from .capability_policy import Rule, decide
lazy from .container_findings import UPDATE
lazy from .infrastructure import ManagedResourceCommand, save_managed_resource, suggest_key
lazy from .payload_words import ChangeRow
lazy from .resource_operations import ACTION_CAPABILITY, OperationAsked, queue_merge

# The input of the playbook that applies a merge: the commit it applies.
REVISION = "revision"
# What a merge's input carries for the run that applies it.
THEN = "then"
_COMMIT = re.compile(COMMIT)


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
            "No adopted playbook applies an app update. On Playbooks, Refresh and adopt the one that does."
        )
    if len(found) > 1:
        names = get_text_list(sorted(manifest.id for manifest in found), "and")
        raise PolicyError(f"More than one adopted playbook applies an app update: {names}. Adopt only one.")
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


def _shown(held: Any) -> ChangePreview:
    """Each pin it moves, and the diff of its Compose files while its head is the one measured."""

    rows = tuple(
        ChangeRow(
            label=f"{pin.get('app', '')} {pin.get('service', '')}".strip(),
            before=str(pin.get("before") or ""),
            after=str(pin.get("after") or ""),
            change="changed" if pin.get("before") else "added",
        )
        for pin in held.baseline.get("pins") or ()
    )
    # Read from what was measured: its subject is the pull request, `owner/name#number`.
    head = held.baseline.get("head")
    patch = "\n".join(
        update.patch
        for update in app_updates()
        if (update.head, f"{update.repository}#{update.number}") == (head, held.resource_key)
    )
    return ChangePreview(label="Pins", rows=rows, lines=tuple(patch.splitlines()))


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


@transaction.atomic
def approve_update(command: ApproveUpdateCommand, *, consented: Consented) -> OperationAsked:
    """Merge the update's pull request at the head reviewed, carrying the run
    that applies the merge once it is made."""

    principal = consent(consented).principal
    manifest = applier()
    _may_run(manifest, principal)
    updates = _updates(command)
    if not updates:
        raise RecordNotFound("No open app update is that pull request.")
    if updates[0].head != command.head_sha:
        raise PolicyError("The update has a new head commit since you reviewed it. Review it again.")
    if not all(update.readable for update in updates):
        raise PolicyError("GitHub gave no diff of this update, so HQ cannot show what it moves. Review it on GitHub.")
    if (updates[0].checks or {}).get("state") != "success":
        raise PolicyError("Its checks are not passing. Approve it once they pass.")
    hosts = resolved_hosts(manifest, manifest.hosts)
    if not hosts:
        raise PolicyError(f"{manifest.id} names no machine to apply an update on.")
    reached(manifest, hosts)
    # The run's record as approved: the follow-up runs only while it still is.
    record_of(manifest, consented=consented)
    correlation = f"{command.repository}#{command.number}@{command.head_sha[:12]}"
    return queue_merge(
        _declared(command, consented=consented),
        consented=consented,
        carries={
            "correlation": correlation,
            THEN: {
                "operation": manifest.id,
                "revision": manifest.revision,
                "hosts": list(hosts),
                "correlation": correlation,
            },
        },
    )


def merged(merge: OperationRequest) -> None:
    """Queue the run a merge carries, now that it made its commit, and say on
    the merge what was queued or why nothing was."""

    then = (merge.input or {}).get(THEN)
    if not isinstance(then, dict):
        return
    sha = str(((merge.result or {}).get("status") or {}).get("merge_sha") or "")
    try:
        with transaction.atomic():
            said = {"run": str(_follow(merge, then, sha).pk)}
    except PolicyError as refused:
        said = {"refused": str(refused)}
    merge.result = {**(merge.result or {}), THEN: said}
    OperationRequest.objects.filter(pk=merge.pk).update(result=merge.result)


def _follow(merge: OperationRequest, then: dict[str, Any], sha: str) -> OperationRequest:
    # GitHub's sha is the one value the approval did not fix.
    if not _COMMIT.fullmatch(sha):
        raise PolicyError("GitHub reported no commit for the merge, so nothing was applied.")
    manifest = operation_registry().get(str(then.get("operation", "")))
    if manifest is None or manifest.revision != then.get("revision"):
        raise PolicyError("The playbook adopted now is not the one approved, so nothing ran.")
    try:
        parameters = manifest.input_type.model_validate({REVISION: sha}).model_dump(mode="json")
    except ValidationError as refused:
        raise PolicyError("The playbook refused the merge commit as its revision, so nothing ran.") from refused
    hosts = [str(host) for host in then.get("hosts") or ()]
    reached(manifest, tuple(hosts))
    bound = {
        "operation": manifest.id,
        "revision": manifest.revision,
        "parameters": parameters,
        "hosts": hosts,
        "correlation": str(then.get("correlation", "")),
    }
    return queue_follow_up(manifest, bound, asked=merge, once=f"{manifest.id}:{sha}")


def capabilities() -> tuple[CapabilitySpec, ...]:
    handler = waits_for_a_person(every_call)(measured_by(_measured, shown=_shown)(approve_update))
    return (
        CapabilitySpec(
            ACTION_CAPABILITY[OperationRequest.Action.MERGE],
            "Approve an app update: merge its pull request at the head you reviewed, then apply the merge "
            "where the app runs.",
            Effect.INFRASTRUCTURE_CHANGE,
            Capability.MANAGE_INFRASTRUCTURE,
            ApproveUpdateCommand,
            handler,
            execution_notes=(
                "Refuse unless one adopted playbook applies app updates and you may run it.",
                "Refuse an update whose head moved, whose diff GitHub did not give, or whose checks are not passing.",
                (
                    "Queue the merge at that head. Once GitHub makes the merge commit, queue one run of that "
                    "playbook with the commit as its revision, as you, joined to the merge by one correlation."
                ),
                "When an agent asks, it waits for an operator's approval.",
            ),
            label="Approve update",
            verb="Approve",
            wants="merge and apply an app update",
        ),
    )
