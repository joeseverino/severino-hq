"""Deterministic JSON capability registry for every HQ adapter."""

from __future__ import annotations

import dataclasses

from dataclasses import dataclass
from functools import cache
import inspect
import re
from typing import Any, Callable

from django.core.exceptions import ImproperlyConfigured
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
    request_removal,
    save_managed_resource,
)
from .projects import ProjectCommand, save_project, upsert_project
from .receipts import ReceiptMetadataCommand, update_receipt
from .security import AuthorizationError, Capability, Principal
from .sync import HQSyncCommand, execute_hq_sync
from .plugins import plugin_capability_specs

CAPABILITY_NAME = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
CAPABILITY_EFFECTS = frozenset(
    {"read", "remote_write", "destructive", "infrastructure_change"}
)


@dataclass(frozen=True)
class TargetKind:
    """How one kind of target reaches the handler that acts on it.

    ``keyword`` is the parameter the host binds it to; ``coerce`` turns the
    transport's value into what that parameter expects.
    """

    keyword: str
    coerce: Callable[[Any], Any]


# One declaration, read three ways: the set of valid kinds (spec validation at
# startup), the keyword each binds to (the handler signature check), and the
# coercion applied when a call arrives. Adding a kind here is the whole change.
TARGET_KINDS: dict[str, TargetKind] = {
    "slug": TargetKind("current_slug", str),
    "doc_id": TargetKind("current_doc_id", str),
    "integer": TargetKind("current_id", int),
    "key": TargetKind("current_key", str),
}


class _UnusableTarget(Exception):
    """The target arrived, but not as the kind the capability declared."""


@dataclass(frozen=True)
class CapabilitySpec:
    name: str
    summary: str
    effect: str
    required_capability: Capability | str | tuple[Capability | str, ...]
    command_type: type
    handler: Callable
    target_kind: str | None = None

    @property
    def required_capabilities(self) -> tuple[Capability | str, ...]:
        if isinstance(self.required_capability, tuple):
            return self.required_capability
        return (self.required_capability,)


_SPECS = (
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
        "infrastructure.resource.remove",
        "Remove the record this declaration describes, then forget it.",
        "destructive",
        Capability.MANAGE_INFRASTRUCTURE,
        OperationCommand,
        request_removal,
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

def capability_specs() -> tuple[CapabilitySpec, ...]:
    specs = (*_SPECS, *plugin_capability_specs())
    for spec in specs:
        _validate_capability_spec(spec)
    names = [spec.name for spec in specs]
    if len(names) != len(set(names)):
        raise ImproperlyConfigured(
            "Duplicate capability name across HQ core and plugins."
        )
    return specs


def _validate_capability_spec(spec: CapabilitySpec) -> None:
    """Fail at registry construction, not on the first production request."""

    if not isinstance(spec, CapabilitySpec):
        raise ImproperlyConfigured(
            "A capability provider returned something other than CapabilitySpec."
        )
    if not CAPABILITY_NAME.fullmatch(spec.name):
        raise ImproperlyConfigured(f"Invalid capability name {spec.name!r}.")
    if not spec.summary.strip():
        raise ImproperlyConfigured(f"Capability {spec.name!r} has no summary.")
    if spec.effect not in CAPABILITY_EFFECTS:
        raise ImproperlyConfigured(
            f"Capability {spec.name!r} has invalid effect {spec.effect!r}."
        )
    if spec.target_kind is not None and spec.target_kind not in TARGET_KINDS:
        raise ImproperlyConfigured(
            f"Capability {spec.name!r} has invalid target {spec.target_kind!r}."
        )
    required = [
        item.value if isinstance(item, Capability) else item
        for item in spec.required_capabilities
    ]
    if not required or any(
        not isinstance(item, str) or not CAPABILITY_NAME.fullmatch(item)
        for item in required
    ):
        raise ImproperlyConfigured(
            f"Capability {spec.name!r} must declare valid required capabilities."
        )
    if len(required) != len(set(required)):
        raise ImproperlyConfigured(
            f"Capability {spec.name!r} repeats a required capability."
        )
    try:
        _command_schema(spec.command_type)
    except Exception as exc:
        raise ImproperlyConfigured(
            f"Capability {spec.name!r} command type cannot emit JSON Schema."
        ) from exc
    if not callable(spec.handler):
        raise ImproperlyConfigured(f"Capability {spec.name!r} handler is not callable.")

    kwargs: dict[str, Any] = {"principal": None, "expected_updated_at": None}
    kind = TARGET_KINDS.get(spec.target_kind) if spec.target_kind else None
    if kind:
        kwargs[kind.keyword] = None
    try:
        inspect.signature(spec.handler).bind(None, **kwargs)
    except TypeError as exc:
        raise ImproperlyConfigured(
            f"Capability {spec.name!r} handler does not implement the host call contract."
        ) from exc


@cache
def _command_schema(command_type: type) -> dict[str, Any]:
    """Build an immutable command type's schema once per process."""

    return TypeAdapter(command_type).json_schema()


def capability_registry() -> dict[str, CapabilitySpec]:
    return {spec.name: spec for spec in capability_specs()}


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
                "summary": spec.summary,
                "effect": spec.effect,
                "required_capabilities": [
                    capability.value if isinstance(capability, Capability) else capability
                    for capability in spec.required_capabilities
                ],
                "target": spec.target_kind,
                "input_schema": _command_schema(spec.command_type),
            }
            for spec in capability_specs()
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
        return _error(exc.code, str(exc))
    except PydanticValidationError as exc:
        return _error("invalid_input", "Payload validation failed.", exc.errors())
    except DjangoValidationError as exc:
        details = getattr(exc, "message_dict", None) or exc.messages
        return _error("invalid_input", "Domain validation failed.", details)
    except (ValueError, TypeError) as exc:
        return _error("operation_failed", str(exc))


def _refuse_unknown_fields(spec: CapabilitySpec, payload: dict[str, Any]) -> None:
    """A field the command does not have is an error, not a no-op.

    Pydantic drops what a dataclass has no room for, so a caller sending a
    misspelled field -- or one this capability used to take and no longer does
    -- got back a success about work it did not ask for.
    """

    known = {field.name for field in dataclasses.fields(spec.command_type)}
    unknown = sorted(set(payload) - known)
    if unknown:
        raise ValueError(
            f"{spec.name} does not take {', '.join(unknown)}."
        )


def _error(code: str, message: str, details: Any = None) -> dict[str, Any]:
    error = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"ok": False, "error": error}
