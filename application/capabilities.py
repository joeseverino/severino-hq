"""Deterministic JSON capability registry for every HQ adapter."""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from pydantic import TypeAdapter, ValidationError as PydanticValidationError

from .assets import AssetCommand, save_asset, upsert_asset
from .content import ContentCommand, save_content
from .contact_submissions import (
    ContactDeleteCommand,
    ContactListCommand,
    ContactReviewCommand,
    execute_contact_delete,
    execute_contact_list,
    execute_contact_review,
)
from .cadence import ControllerSweepCommand, request_controller_sweep
from .deletion import (
    DeleteCommand,
    delete_asset,
    delete_content,
    delete_documentation,
    delete_expense,
    delete_project,
    delete_receipt,
)
from .documentation import (
    DocumentationCommand,
    DocumentationSyncCommand,
    execute_documentation_sync,
    save_documentation,
)
from .expenses import ExpenseCommand, save_expense
from .infrastructure import (
    CERTIFICATE_KIND,
    ManagedResourceCommand,
    OperationCommand,
    request_certificate_renewal,
    request_route_approval,
    request_reconcile,
    request_removal,
    save_managed_resource,
)
from .integration_specs import TARGET_KINDS, CapabilitySpec, command_schema
from .lookup import (
    AddressCommand,
    NameCommand,
    look_up_address,
    look_up_name,
)
from .projects import (
    ProjectCommand,
    ProjectRefreshCommand,
    execute_project_refresh,
    save_project,
    upsert_project,
)
from .receipts import ReceiptMetadataCommand, update_receipt
from .integrations import integration_graph
from .security import AuthorizationError, Capability, Principal
from .sync import HQSyncCommand, execute_hq_sync


class _UnusableTarget(Exception):
    """The target arrived, but not as the kind the capability declared."""


CORE_CAPABILITY_SPECS = (
    CapabilitySpec(
        "hq.sync",
        "Atomically synchronize the vault manifest into HQ.",
        "remote_write",
        # Documentation authority alone. It also required infrastructure
        # authority while it carried a topology; keeping that would mean an
        # account allowed to sync docs and nothing else could not, and the only
        # way to let it would be to hand it the whole control plane.
        Capability.SYNC_DOCUMENTATION,
        HQSyncCommand,
        execute_hq_sync,
        subject_resource="documentation",
    ),
    CapabilitySpec(
        "project.create",
        "Create an HQ project.",
        "remote_write",
        Capability.WRITE_PROJECTS,
        ProjectCommand,
        save_project,
        subject_resource="projects",
    ),
    CapabilitySpec(
        "project.upsert",
        "Idempotently create or update an HQ project by slug.",
        "remote_write",
        Capability.WRITE_PROJECTS,
        ProjectCommand,
        upsert_project,
        subject_resource="projects",
    ),
    CapabilitySpec(
        "project.update",
        "Update an HQ project.",
        "remote_write",
        Capability.WRITE_PROJECTS,
        ProjectCommand,
        save_project,
        "slug",
        "projects",
        target_label="Project slug",
        target_help="The project to update.",
    ),
    CapabilitySpec(
        "project.refresh",
        "Refresh a project's GitHub and published-content metadata.",
        "remote_write",
        Capability.WRITE_PROJECTS,
        ProjectRefreshCommand,
        execute_project_refresh,
        "slug",
        "projects",
        target_label="Project slug",
        target_help="The project whose external metadata to refresh.",
        execution_notes=(
            "Read the selected project's registered repository URL.",
            "Ask GitHub for current push metadata using the configured connection.",
            "Persist the observed timestamp and attribute the refresh to this operator.",
        ),
    ),
    CapabilitySpec(
        "contact.submissions.list",
        "List contact submissions held in Cloudflare D1.",
        "read",
        Capability.MANAGE_CONTACTS,
        ContactListCommand,
        execute_contact_list,
        subject_resource="contact.submissions",
        execution_notes=(
            "Validate the requested status and result bound locally.",
            "Read submissions through the configured D1 connection.",
            "Return only the requested bounded result set.",
        ),
    ),
    CapabilitySpec(
        "contact.submission.review",
        "Review and update one contact submission in Cloudflare D1.",
        "remote_write",
        Capability.MANAGE_CONTACTS,
        ContactReviewCommand,
        execute_contact_review,
        "integer",
        "contact.submissions",
        target_label="Submission ID",
        target_help="The contact submission to review.",
        execution_notes=(
            "Read the selected submission and validate its new review state.",
            "Write the review fields through the configured D1 connection.",
            "Record the attributed change in HQ's audit log.",
        ),
    ),
    CapabilitySpec(
        "contact.submission.delete",
        "Delete one explicitly confirmed contact submission from Cloudflare D1.",
        "destructive",
        Capability.MANAGE_CONTACTS,
        ContactDeleteCommand,
        execute_contact_delete,
        "integer",
        "contact.submissions",
        target_label="Submission ID",
        target_help="The contact submission to delete.",
        execution_notes=(
            "Require confirmation that exactly matches the selected submission ID.",
            "Delete the record through the configured D1 connection.",
            "Treat an already-absent record as a successful retry and audit the change.",
        ),
    ),
    CapabilitySpec(
        "asset.create",
        "Create an HQ asset.",
        "remote_write",
        Capability.WRITE_ASSETS,
        AssetCommand,
        save_asset,
        subject_resource="assets",
    ),
    CapabilitySpec(
        "asset.upsert",
        "Idempotently create or update an HQ asset by slug.",
        "remote_write",
        Capability.WRITE_ASSETS,
        AssetCommand,
        upsert_asset,
        subject_resource="assets",
    ),
    CapabilitySpec(
        "asset.update",
        "Update an HQ asset.",
        "remote_write",
        Capability.WRITE_ASSETS,
        AssetCommand,
        save_asset,
        "slug",
        "assets",
        target_label="Asset slug",
        target_help="The asset to update.",
    ),
    CapabilitySpec(
        "content.create",
        "Create an HQ content item.",
        "remote_write",
        Capability.WRITE_CONTENT,
        ContentCommand,
        save_content,
        subject_resource="content",
    ),
    CapabilitySpec(
        "content.update",
        "Update an HQ content item.",
        "remote_write",
        Capability.WRITE_CONTENT,
        ContentCommand,
        save_content,
        "slug",
        "content",
        target_label="Content slug",
        target_help="The content item to update.",
    ),
    CapabilitySpec(
        "expense.create",
        "Create an HQ expense.",
        "remote_write",
        Capability.WRITE_EXPENSES,
        ExpenseCommand,
        save_expense,
        subject_resource="expenses",
    ),
    CapabilitySpec(
        "expense.update",
        "Update an HQ expense.",
        "remote_write",
        Capability.WRITE_EXPENSES,
        ExpenseCommand,
        save_expense,
        "integer",
        "expenses",
        target_label="Expense ID",
        target_help="The expense to update.",
    ),
    CapabilitySpec(
        "documentation.create",
        "Create an HQ documentation metadata record.",
        "remote_write",
        Capability.WRITE_DOCUMENTATION,
        DocumentationCommand,
        save_documentation,
        subject_resource="documentation",
    ),
    CapabilitySpec(
        "documentation.update",
        "Update an HQ documentation metadata record.",
        "remote_write",
        Capability.WRITE_DOCUMENTATION,
        DocumentationCommand,
        save_documentation,
        "doc_id",
        "documentation",
        target_label="Document ID",
        target_help="The documentation record to update.",
    ),
    CapabilitySpec(
        "documentation.sync",
        "Synchronize a validated vault manifest into HQ.",
        "remote_write",
        Capability.SYNC_DOCUMENTATION,
        DocumentationSyncCommand,
        execute_documentation_sync,
        subject_resource="documentation",
    ),
    CapabilitySpec(
        "receipt.update",
        "Update receipt metadata and relationships (never file bytes).",
        "remote_write",
        Capability.WRITE_RECEIPTS,
        ReceiptMetadataCommand,
        update_receipt,
        "integer",
        "receipts",
        target_label="Receipt ID",
        target_help="The receipt to update.",
    ),
    CapabilitySpec(
        "project.delete",
        "Delete a confirmed project.",
        "destructive",
        Capability.DELETE_PROJECTS,
        DeleteCommand,
        delete_project,
        "slug",
        "projects",
        target_label="Project slug",
        target_help="The project to delete.",
    ),
    CapabilitySpec(
        "asset.delete",
        "Delete a confirmed asset.",
        "destructive",
        Capability.DELETE_ASSETS,
        DeleteCommand,
        delete_asset,
        "slug",
        "assets",
        target_label="Asset slug",
        target_help="The asset to delete.",
    ),
    CapabilitySpec(
        "content.delete",
        "Delete confirmed content.",
        "destructive",
        Capability.DELETE_CONTENT,
        DeleteCommand,
        delete_content,
        "slug",
        "content",
        target_label="Content slug",
        target_help="The content item to delete.",
    ),
    CapabilitySpec(
        "expense.delete",
        "Delete a confirmed expense.",
        "destructive",
        Capability.DELETE_EXPENSES,
        DeleteCommand,
        delete_expense,
        "integer",
        "expenses",
        target_label="Expense ID",
        target_help="The expense to delete.",
    ),
    CapabilitySpec(
        "documentation.delete",
        "Delete confirmed documentation metadata.",
        "destructive",
        Capability.DELETE_DOCUMENTATION,
        DeleteCommand,
        delete_documentation,
        "doc_id",
        "documentation",
        target_label="Document ID",
        target_help="The documentation record to delete.",
    ),
    CapabilitySpec(
        "receipt.delete",
        "Delete a confirmed receipt and its private file.",
        "destructive",
        Capability.DELETE_RECEIPTS,
        DeleteCommand,
        delete_receipt,
        "integer",
        "receipts",
        target_label="Receipt ID",
        target_help="The receipt to delete.",
    ),
    CapabilitySpec(
        "infrastructure.resource.create",
        "Declare a typed managed infrastructure resource.",
        "remote_write",
        Capability.MANAGE_INFRASTRUCTURE,
        ManagedResourceCommand,
        save_managed_resource,
        subject_resource="infrastructure.resources",
    ),
    CapabilitySpec(
        "infrastructure.resource.update",
        "Update a typed managed infrastructure resource.",
        "remote_write",
        Capability.MANAGE_INFRASTRUCTURE,
        ManagedResourceCommand,
        save_managed_resource,
        "key",
        "infrastructure.resources",
        target_label="Resource key",
        target_help="The managed infrastructure resource to update.",
        target_initial_fields=("key", "kind", "spec", "enabled"),
    ),
    CapabilitySpec(
        "infrastructure.reconcile",
        "Queue reconciliation of one managed infrastructure resource.",
        "infrastructure_change",
        Capability.MANAGE_INFRASTRUCTURE,
        OperationCommand,
        request_reconcile,
        "key",
        "infrastructure.resources",
        target_label="Resource key",
        target_help="The managed infrastructure resource to reconcile.",
    ),
    CapabilitySpec(
        "infrastructure.controller.refresh",
        "Wake the privileged controller to pull work and refresh due observations.",
        "infrastructure_change",
        Capability.MANAGE_INFRASTRUCTURE,
        ControllerSweepCommand,
        request_controller_sweep,
        execution_notes=(
            "Mark HQ active so the short observation cadence applies.",
            "Ring the credential-free controller doorbell; no provider authority enters the web process.",
            "The privileged controller pulls its own contract and refreshes only what HQ says is due.",
        ),
    ),
    CapabilitySpec(
        "infrastructure.resource.remove",
        "Remove the record this declaration describes, then forget it.",
        "destructive",
        Capability.MANAGE_INFRASTRUCTURE,
        OperationCommand,
        request_removal,
        "key",
        "infrastructure.resources",
        target_label="Resource key",
        target_help="The managed infrastructure resource to remove.",
    ),
    CapabilitySpec(
        "tailnet.routes.approve",
        "Approve the routes a tailnet device already advertises.",
        "infrastructure_change",
        Capability.MANAGE_INFRASTRUCTURE,
        OperationCommand,
        request_route_approval,
        "key",
        "infrastructure.resources",
        target_label="Device key",
        target_help="The tailnet device whose advertised routes to approve.",
        target_query=(("kind", "tailscale.device"),),
        execution_notes=(
            "Read what the device currently advertises and what is already approved.",
            "Queue one approval for the controller; the API call runs outside this request.",
            "Approve exactly the advertised set, so no route this was not about is withdrawn.",
        ),
    ),
    CapabilitySpec(
        "certificate.renew",
        "Request certificate renewal when policy allows it.",
        "infrastructure_change",
        Capability.REQUEST_CERTIFICATE_RENEWAL,
        OperationCommand,
        request_certificate_renewal,
        "key",
        "infrastructure.resources",
        target_label="Certificate key",
        target_help="The managed certificate to renew.",
        target_query=(("kind", CERTIFICATE_KIND),),
        execution_notes=(
            "Read the selected certificate declaration and evaluate renewal policy.",
            "Queue one renewal request for the controller; provider work runs outside this page request.",
            "Return the queued operation and policy decision, attributed to this operator.",
        ),
    ),
    # The first two capabilities that read something HQ does not hold. Both are
    # `read`, so neither takes an idempotency key and neither writes: asking a
    # registry the same question twice is the same question twice.
    CapabilitySpec(
        "lookup.name",
        "Ask a public resolver what the internet returns for a hostname.",
        "read",
        Capability.LOOK_UP_PUBLIC_RECORDS,
        NameCommand,
        look_up_name,
        execution_notes=(
            "Validate the hostname before anything leaves this machine.",
            "Ask one resolver outside this network, so internal rewrites cannot "
            "answer a question about the public internet.",
            "Return the records as the resolver gave them, with no TTL: this "
            "provider reports a constant, which is not a measurement.",
        ),
    ),
    CapabilitySpec(
        "lookup.address",
        "Ask what name and which allocation a public address belongs to.",
        "read",
        Capability.LOOK_UP_PUBLIC_RECORDS,
        AddressCommand,
        look_up_address,
        execution_notes=(
            "Refuse a private address locally; nothing outside can describe it, "
            "and asking would disclose it for no answer.",
            "Read reverse DNS, which the address holder publishes and which "
            "usually carries a brand name.",
            "Read the RDAP allocation, which the registry publishes and which "
            "carries the company. Either registry may fail without the other.",
        ),
    ),
)


def capability_label(name: str) -> str:
    """Human label for a stable dotted capability name."""

    words = name.replace(".", " ").replace("_", " ").split()
    return " ".join(word.upper() if len(word) <= 3 else word.title() for word in words)


def capability_registry() -> dict[str, CapabilitySpec]:
    return dict(integration_graph().capabilities)


def authorize_capability(spec: CapabilitySpec, principal: Principal) -> None:
    """Apply the registry's one authorization rule for every adapter."""

    for capability in spec.required_capabilities:
        principal.require(capability)


def describe_capabilities() -> dict[str, Any]:
    """Return stable JSON Schemas and operational effects for every capability."""

    return {
        "ok": True,
        "schema_version": 2,
        "capabilities": [
            {
                "name": spec.name,
                "label": capability_label(spec.name),
                "summary": spec.summary,
                "effect": spec.effect,
                "required_capabilities": [
                    capability.value
                    if isinstance(capability, Capability)
                    else capability
                    for capability in spec.required_capabilities
                ],
                "target": spec.target_kind,
                "target_label": spec.target_label,
                "target_help": spec.target_help,
                "target_query": dict(spec.target_query),
                "execution_notes": list(spec.execution_notes),
                "target_initial_fields": list(spec.target_initial_fields),
                "resource": spec.subject_resource,
                "input_schema": command_schema(spec.command_type),
            }
            for spec in integration_graph().capabilities.values()
        ],
    }


def _target_keyword(spec: CapabilitySpec, target: str | int | None) -> dict[str, Any]:
    """Bind the target to the keyword its capability declared it under."""

    if not spec.target_kind:
        return {}
    kind = TARGET_KINDS[spec.target_kind]
    try:
        return {kind.keyword: kind.coerce(target)}
    except (ValueError, TypeError) as exc:
        raise _UnusableTarget from exc


def execute_capability(
    name: str,
    payload: dict[str, Any],
    *,
    principal: Principal,
    target: str | int | None = None,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Validate JSON and execute one allowlisted application capability."""

    spec = capability_registry().get(name)
    if spec is None:
        return _error("unknown_capability", f"Unknown capability {name!r}.")
    if spec.target_kind and target is None:
        return _error("target_required", f"{name} requires a target.")
    if not spec.target_kind and target is not None:
        return _error("target_not_allowed", f"{name} does not accept a target.")

    try:
        # Authority first, then the payload, then the target. A caller who may
        # not run this at all is told exactly that, and learns nothing about
        # what shape of target it would have taken.
        authorize_capability(spec, principal)
        _refuse_unknown_fields(spec, payload)
        command = TypeAdapter(spec.command_type).validate_python(payload)
        kwargs: dict[str, Any] = {
            "principal": principal,
            "expected_updated_at": expected_updated_at,
            **_target_keyword(spec, target),
        }
        return spec.handler(command, **kwargs)
    except _UnusableTarget:
        return _error("invalid_input", f"{name} requires a {spec.target_kind} target.")
    except AuthorizationError as exc:
        return _error(exc.code, exc.reason)
    except PydanticValidationError as exc:
        return _error("invalid_input", "Payload validation failed.", exc.errors())
    except DjangoValidationError as exc:
        details = getattr(exc, "message_dict", None) or exc.messages
        return _error("invalid_input", "Domain validation failed.", details)
    except (TypeError, ValueError):
        # Neither handler nor dependency exception text crosses an adapter.
        # It can contain argument names, provider responses, paths, or values
        # from the request. The capability name is registry-owned and safe.
        return _error("operation_failed", f"{name} could not be executed.")


def _refuse_unknown_fields(spec: CapabilitySpec, payload: dict[str, Any]) -> None:
    """A field the command does not have is an error, not a no-op.

    A caller sending a
    misspelled field -- or one this capability used to take and no longer does
    -- got back a success about work it did not ask for.
    """

    # CapabilitySpec accepts host dataclasses and plugin StrictCommand models.
    # Their JSON Schema is already the shared adapter contract, so it is also
    # the single source of field names.
    known = set(command_schema(spec.command_type).get("properties", {}))
    unknown = sorted(set(payload) - known)
    if unknown:
        raise ValueError(f"{spec.name} does not take {', '.join(unknown)}.")


def _error(code: str, message: str, details: Any = None) -> dict[str, Any]:
    error = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"ok": False, "error": error}
