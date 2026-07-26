"""Deterministic JSON capability registry for every HQ adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from django.core.exceptions import ValidationError as DjangoValidationError
from pydantic import TypeAdapter, ValidationError as PydanticValidationError

from .assets import AssetCommand, save_asset, upsert_asset
from .content import ContentCommand, save_content
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
    ManagedResourceCommand,
    OperationCommand,
    request_certificate_renewal,
    request_reconcile,
    save_managed_resource,
)
from .projects import ProjectCommand, save_project, upsert_project
from .receipts import ReceiptMetadataCommand, update_receipt
from .security import AuthorizationError, Capability, Principal
from .sync import HQSyncCommand, execute_hq_sync
from .topology import TopologySyncCommand, execute_topology_sync


@dataclass(frozen=True)
class CapabilitySpec:
    name: str
    summary: str
    effect: str
    required_capability: Capability | tuple[Capability, ...]
    command_type: type
    handler: Callable
    target_kind: str | None = None

    @property
    def required_capabilities(self) -> tuple[Capability, ...]:
        if isinstance(self.required_capability, tuple):
            return self.required_capability
        return (self.required_capability,)


_SPECS = (
    CapabilitySpec(
        "hq.sync",
        "Atomically synchronize the vault manifest and topology into HQ.",
        "remote_write",
        (Capability.SYNC_DOCUMENTATION, Capability.MANAGE_INFRASTRUCTURE),
        HQSyncCommand,
        execute_hq_sync,
    ),
    CapabilitySpec(
        "project.create",
        "Create an HQ project.",
        "remote_write",
        Capability.WRITE_PROJECTS,
        ProjectCommand,
        save_project,
    ),
    CapabilitySpec(
        "project.upsert",
        "Idempotently create or update an HQ project by slug.",
        "remote_write",
        Capability.WRITE_PROJECTS,
        ProjectCommand,
        upsert_project,
    ),
    CapabilitySpec(
        "project.update",
        "Update an HQ project.",
        "remote_write",
        Capability.WRITE_PROJECTS,
        ProjectCommand,
        save_project,
        "slug",
    ),
    CapabilitySpec(
        "asset.create",
        "Create an HQ asset.",
        "remote_write",
        Capability.WRITE_ASSETS,
        AssetCommand,
        save_asset,
    ),
    CapabilitySpec(
        "asset.upsert",
        "Idempotently create or update an HQ asset by slug.",
        "remote_write",
        Capability.WRITE_ASSETS,
        AssetCommand,
        upsert_asset,
    ),
    CapabilitySpec(
        "asset.update",
        "Update an HQ asset.",
        "remote_write",
        Capability.WRITE_ASSETS,
        AssetCommand,
        save_asset,
        "slug",
    ),
    CapabilitySpec(
        "content.create",
        "Create an HQ content item.",
        "remote_write",
        Capability.WRITE_CONTENT,
        ContentCommand,
        save_content,
    ),
    CapabilitySpec(
        "content.update",
        "Update an HQ content item.",
        "remote_write",
        Capability.WRITE_CONTENT,
        ContentCommand,
        save_content,
        "slug",
    ),
    CapabilitySpec(
        "expense.create",
        "Create an HQ expense.",
        "remote_write",
        Capability.WRITE_EXPENSES,
        ExpenseCommand,
        save_expense,
    ),
    CapabilitySpec(
        "expense.update",
        "Update an HQ expense.",
        "remote_write",
        Capability.WRITE_EXPENSES,
        ExpenseCommand,
        save_expense,
        "integer",
    ),
    CapabilitySpec(
        "documentation.create",
        "Create an HQ documentation metadata record.",
        "remote_write",
        Capability.WRITE_DOCUMENTATION,
        DocumentationCommand,
        save_documentation,
    ),
    CapabilitySpec(
        "documentation.update",
        "Update an HQ documentation metadata record.",
        "remote_write",
        Capability.WRITE_DOCUMENTATION,
        DocumentationCommand,
        save_documentation,
        "doc_id",
    ),
    CapabilitySpec(
        "documentation.sync",
        "Synchronize a validated vault manifest into HQ.",
        "remote_write",
        Capability.SYNC_DOCUMENTATION,
        DocumentationSyncCommand,
        execute_documentation_sync,
    ),
    CapabilitySpec(
        "infrastructure.topology.sync",
        "Synchronize the validated infrastructure topology into HQ.",
        "remote_write",
        Capability.MANAGE_INFRASTRUCTURE,
        TopologySyncCommand,
        execute_topology_sync,
    ),
    CapabilitySpec(
        "receipt.update",
        "Update receipt metadata and relationships (never file bytes).",
        "remote_write",
        Capability.WRITE_RECEIPTS,
        ReceiptMetadataCommand,
        update_receipt,
        "integer",
    ),
    CapabilitySpec(
        "project.delete",
        "Delete a confirmed project.",
        "destructive",
        Capability.DELETE_PROJECTS,
        DeleteCommand,
        delete_project,
        "slug",
    ),
    CapabilitySpec(
        "asset.delete",
        "Delete a confirmed asset.",
        "destructive",
        Capability.DELETE_ASSETS,
        DeleteCommand,
        delete_asset,
        "slug",
    ),
    CapabilitySpec(
        "content.delete",
        "Delete confirmed content.",
        "destructive",
        Capability.DELETE_CONTENT,
        DeleteCommand,
        delete_content,
        "slug",
    ),
    CapabilitySpec(
        "expense.delete",
        "Delete a confirmed expense.",
        "destructive",
        Capability.DELETE_EXPENSES,
        DeleteCommand,
        delete_expense,
        "integer",
    ),
    CapabilitySpec(
        "documentation.delete",
        "Delete confirmed documentation metadata.",
        "destructive",
        Capability.DELETE_DOCUMENTATION,
        DeleteCommand,
        delete_documentation,
        "doc_id",
    ),
    CapabilitySpec(
        "receipt.delete",
        "Delete a confirmed receipt and its private file.",
        "destructive",
        Capability.DELETE_RECEIPTS,
        DeleteCommand,
        delete_receipt,
        "integer",
    ),
    CapabilitySpec(
        "infrastructure.resource.create",
        "Declare a typed managed infrastructure resource.",
        "remote_write",
        Capability.MANAGE_INFRASTRUCTURE,
        ManagedResourceCommand,
        save_managed_resource,
    ),
    CapabilitySpec(
        "infrastructure.resource.update",
        "Update a typed managed infrastructure resource.",
        "remote_write",
        Capability.MANAGE_INFRASTRUCTURE,
        ManagedResourceCommand,
        save_managed_resource,
        "key",
    ),
    CapabilitySpec(
        "infrastructure.reconcile",
        "Queue reconciliation of one managed infrastructure resource.",
        "infrastructure_change",
        Capability.MANAGE_INFRASTRUCTURE,
        OperationCommand,
        request_reconcile,
        "key",
    ),
    CapabilitySpec(
        "certificate.renew",
        "Request certificate renewal when policy allows it.",
        "infrastructure_change",
        Capability.REQUEST_CERTIFICATE_RENEWAL,
        OperationCommand,
        request_certificate_renewal,
        "key",
    ),
)

REGISTRY = {spec.name: spec for spec in _SPECS}


def describe_capabilities() -> dict[str, Any]:
    """Return stable JSON Schemas and operational effects for every capability."""

    return {
        "ok": True,
        "schema_version": 2,
        "capabilities": [
            {
                "name": spec.name,
                "summary": spec.summary,
                "effect": spec.effect,
                "required_capabilities": [
                    capability.value for capability in spec.required_capabilities
                ],
                "target": spec.target_kind,
                "input_schema": TypeAdapter(spec.command_type).json_schema(),
            }
            for spec in _SPECS
        ],
    }


def execute_capability(
    name: str,
    payload: dict[str, Any],
    *,
    principal: Principal,
    target: str | int | None = None,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Validate JSON and execute one allowlisted application capability."""

    spec = REGISTRY.get(name)
    if spec is None:
        return _error("unknown_capability", f"Unknown capability {name!r}.")
    if spec.target_kind and target is None:
        return _error("target_required", f"{name} requires a target.")
    if not spec.target_kind and target is not None:
        return _error("target_not_allowed", f"{name} does not accept a target.")

    try:
        for capability in spec.required_capabilities:
            principal.require(capability)
        command = TypeAdapter(spec.command_type).validate_python(payload)
        kwargs: dict[str, Any] = {
            "principal": principal,
            "expected_updated_at": expected_updated_at,
        }
        if spec.target_kind == "slug":
            kwargs["current_slug"] = str(target)
        elif spec.target_kind == "doc_id":
            kwargs["current_doc_id"] = str(target)
        elif spec.target_kind == "integer":
            kwargs["current_id"] = int(target)
        elif spec.target_kind == "key":
            kwargs["current_key"] = str(target)
        return spec.handler(command, **kwargs)
    except AuthorizationError as exc:
        return _error(exc.code, str(exc))
    except PydanticValidationError as exc:
        return _error("invalid_input", "Payload validation failed.", exc.errors())
    except DjangoValidationError as exc:
        details = getattr(exc, "message_dict", None) or exc.messages
        return _error("invalid_input", "Domain validation failed.", details)
    except (ValueError, TypeError) as exc:
        return _error("operation_failed", str(exc))


def _error(code: str, message: str, details: Any = None) -> dict[str, Any]:
    error = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"ok": False, "error": error}
