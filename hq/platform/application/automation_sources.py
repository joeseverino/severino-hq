"""Refresh: asking the automation worker what procedures the repository it
holds declares, and storing what it found as a proposal HQ judged.

Nothing is fetched here. A Refresh queues one scan of the automation source
(``automation.source:scan``) and rings the worker's doorbell; the worker reads
the tip of the repository's default branch with the token only it holds and
reports a catalog over the bridge (``SourceCatalog``). HQ holds each sidecar
to ``ProcedureDeclaration``, the model it has one home in, and stores the
catalog with every way a procedure falls short. A proposal is never adopted
and never runnable. How the scan reads a repository: docs/Architecture.md,
"Refreshing the procedures".
"""

import json
import re
import secrets
from collections import Counter
from typing import Annotated, Any

from django.db import transaction
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    StringConstraints,
    ValidationError,
)

lazy from hq.domains.control_plane.models import ManagedResource, OperationRequest, SourceProposal
lazy from hq.domains.control_plane.provider_adapters.automation import SOURCE_KIND
lazy from hq.platform.core.audit import audit_operation

from .automation_manifest import DottedName, ProcedureTerms
from .command_fields import Describe
from .integration_specs import CapabilitySpec
from .security import Capability, Principal
from .ui import ended
lazy from .cadence import ring_automation_doorbell
lazy from .idempotency import IdempotencyConflict
lazy from .infrastructure import ManagedResourceCommand, save_managed_resource
lazy from .resource_operations import OperationQueued, enqueue, serialize_operation

REFRESH = "automation.source.refresh"
# The one source's record: the repository is the host's configuration, so
# HQ knows of one source and never which repository it is.
SOURCE_KEY = "automation-source"
# As the bridge contract's ``ScannedProcedure`` bounds a procedure's problems.
PROBLEM_LIMIT = 500
MOST_PROBLEMS = 20


def _pattern(value: str) -> str:
    try:
        re.compile(value)
    except re.error as exc:
        raise ValueError(f"{value!r} is not a regular expression: {exc}") from None
    return value


class InputBound(BaseModel):
    """What Ansible's argument spec cannot say of a text input: the shape it
    must have and how long it may be."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    pattern: Annotated[str, StringConstraints(min_length=1, max_length=500), AfterValidator(_pattern)]
    max_length: Annotated[int, Field(ge=1, le=4096)]


class ProcedureDeclaration(ProcedureTerms):
    """A procedure's sidecar (``<name>.hq.yml`` beside ``<name>.yml``): what
    HQ needs to know of it that Ansible has no word for. Its title, its
    preconditions and verification and its inputs' types are Ansible's own,
    read from the playbook and its roles."""

    # A sidecar that names none escalates through none; a manifest says so
    # outright, since an extension's author writes it by hand.
    privilege: DottedName | None = None
    # The bounds of each text input its roles' argument specs declare, by name.
    inputs: dict[Annotated[str, StringConstraints(pattern=r"^[a-z_][a-z0-9_]{0,63}$")], InputBound] = Field(
        default_factory=dict
    )


def _clip(text: str) -> str:
    return text if len(text) <= PROBLEM_LIMIT else text[: PROBLEM_LIMIT - 1] + "…"


def _declaration_problems(declaration: Any) -> list[str]:
    if declaration is None:
        return []
    try:
        # As JSON, which the sidecar was read into: a list is a strict tuple there.
        ProcedureDeclaration.model_validate_json(json.dumps(declaration))
    except ValidationError as exc:
        return [
            _clip(ended(f"Its sidecar's {'.'.join(str(part) for part in error['loc']) or 'value'}: {error['msg']}"))
            for error in exc.errors(include_url=False)
        ]
    return []


def judged(procedures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each procedure as the worker reported it, with every way HQ finds it
    falls short of a procedure it could adopt added to its problems."""

    ids = Counter(
        procedure["declaration"].get("id") for procedure in procedures if isinstance(procedure.get("declaration"), dict)
    )
    found = []
    for procedure in procedures:
        problems = [*procedure["problems"], *_declaration_problems(procedure["declaration"])]
        declaration = procedure["declaration"]
        named = declaration.get("id") if isinstance(declaration, dict) else None
        if isinstance(named, str) and ids[named] > 1:
            problems.append(f"Another sidecar names the id {named} too.")
        found.append({**procedure, "problems": problems[:MOST_PROBLEMS]})
    return found


def propose(operation: OperationRequest, catalog: dict[str, Any]) -> SourceProposal:
    """Store what one scan found, held to the bridge contract at its door."""

    verification = catalog["verification"]
    return SourceProposal.objects.create(
        operation=operation,
        branch=catalog["branch"],
        revision=catalog["revision"],
        verified=verification["verified"],
        verification_reason=verification["reason"],
        committer=verification["committer"],
        procedures=judged(catalog["procedures"]),
        files=catalog["files"],
        unpinned=catalog["unpinned"],
    )


class RefreshCommand(BaseModel):
    """A Refresh takes nothing but why it was asked and its retry key: the
    repository is the host's configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: Annotated[StrictStr, Field(max_length=300), Describe("Why it is refreshed, kept with the scan.")] = ""
    # Filled by the capability runner with the caller's key, or a fresh one.
    idempotency_key: StrictStr = ""


def _source(principal: Principal) -> ManagedResource:
    current = ManagedResource.objects.select_for_update().filter(key=SOURCE_KEY).first()
    if current is None:
        save_managed_resource(
            ManagedResourceCommand(key=SOURCE_KEY, kind=SOURCE_KIND, spec={"named_by": "host"}), principal=principal
        )
    return ManagedResource.objects.select_for_update().get(key=SOURCE_KEY)


@transaction.atomic
def refresh(command: RefreshCommand, *, principal: Principal) -> OperationQueued:
    """Queue one scan of the automation source for the worker, once per retry
    key, and answer the one already waiting while there is one."""

    key = command.idempotency_key or f"refresh:{secrets.token_urlsafe(18)}"
    existing = OperationRequest.objects.filter(idempotency_key=key).select_related("resource").first()
    if existing is not None:
        if existing.action != OperationRequest.Action.SCAN:
            raise IdempotencyConflict("That retry key was already used for a different request.")
        return {"ok": True, "queued": False, "operation": serialize_operation(existing)}
    with audit_operation(operation=REFRESH, principal=principal):
        resource = _source(principal)
        operation, created = enqueue(
            resource,
            OperationRequest.Action.SCAN,
            idempotency_key=key,
            requested_actor=principal.actor,
            requested_interface=principal.interface,
            requested_device=principal.device,
            requested_person_key=principal.person_key,
            reason=command.reason,
            input={"generation": resource.generation},
        )
    if created:
        # The worker pulls the scan over the bridge; the ring only says one
        # waits, and only once it is stored for the claim to find.
        transaction.on_commit(ring_automation_doorbell)
    return {"ok": True, "queued": created, "operation": serialize_operation(operation)}


def capability_specs() -> tuple[CapabilitySpec, ...]:
    return (
        CapabilitySpec(
            REFRESH,
            "Ask the automation worker which procedures its repository holds, and keep what it finds as a "
            "proposal. Nothing it finds can run until it is adopted.",
            "remote_write",
            Capability.MANAGE_INFRASTRUCTURE,
            RefreshCommand,
            refresh,
            execution_notes=(
                "Queue one scan for the automation worker, or answer the one already waiting.",
                "The worker reads the tip of the repository's default branch and runs nothing of it.",
                "Keep what it reports as a proposal, with each way a procedure falls short.",
            ),
            label="Refresh the procedures",
            verb=OperationRequest.Action.SCAN.label,
            wants="read which procedures the automation repository holds",
        ),
    )
