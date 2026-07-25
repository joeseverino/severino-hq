"""Deterministic JSON capability registry for every HQ adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from django.core.exceptions import ValidationError as DjangoValidationError
from pydantic import TypeAdapter, ValidationError as PydanticValidationError

from .assets import AssetCommand, save_asset
from .content import ContentCommand, save_content
from .documentation import DocumentationSyncCommand, execute_documentation_sync
from .expenses import ExpenseCommand, save_expense
from .projects import ProjectCommand, save_project
from .receipts import ReceiptMetadataCommand, update_receipt
from .security import AuthorizationError, Capability, Principal


@dataclass(frozen=True)
class CapabilitySpec:
    name: str
    summary: str
    effect: str
    required_capability: Capability
    command_type: type
    handler: Callable
    target_kind: str | None = None


_SPECS = (
    CapabilitySpec(
        "project.create",
        "Create an HQ project.",
        "remote_write",
        Capability.WRITE_PROJECTS,
        ProjectCommand,
        save_project,
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
        "documentation.sync",
        "Synchronize a validated vault manifest into HQ.",
        "remote_write",
        Capability.SYNC_DOCUMENTATION,
        DocumentationSyncCommand,
        execute_documentation_sync,
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
)

REGISTRY = {spec.name: spec for spec in _SPECS}


def describe_capabilities() -> dict[str, Any]:
    """Return stable JSON Schemas and operational effects for every capability."""

    return {
        "ok": True,
        "schema_version": 1,
        "capabilities": [
            {
                "name": spec.name,
                "summary": spec.summary,
                "effect": spec.effect,
                "required_capability": spec.required_capability.value,
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
        command = TypeAdapter(spec.command_type).validate_python(payload)
        kwargs: dict[str, Any] = {
            "principal": principal,
            "expected_updated_at": expected_updated_at,
        }
        if spec.target_kind == "slug":
            kwargs["current_slug"] = str(target)
        elif spec.target_kind == "integer":
            kwargs["current_id"] = int(target)
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
