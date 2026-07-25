"""Explicit, confirmed delete capabilities for HQ domain records."""

from dataclasses import dataclass

from django.db import transaction

from assets.models import Asset
from content.models import ContentItem
from core.audit import operation_context
from docs_index.models import DocumentationRecord
from expenses.models import Expense
from projects.models import Project
from receipts.models import Receipt

from .security import Capability, Principal


@dataclass(frozen=True)
class DeleteCommand:
    confirm: str


class ConflictError(ValueError):
    """The caller tried to delete a newer version of an object."""


def _delete(
    model,
    *,
    lookup: dict,
    target: str,
    command: DeleteCommand,
    principal: Principal,
    capability: Capability,
    operation: str,
    type_name: str,
    expected_updated_at: str | None = None,
    after_commit=None,
):
    principal.require(capability)
    if command.confirm != target:
        raise ValueError(f"confirm must exactly match target {target!r}")
    with transaction.atomic(), operation_context(
        interface=principal.interface, actor=principal.actor, operation=operation
    ):
        try:
            obj = model.objects.select_for_update().get(**lookup)
        except model.DoesNotExist as exc:
            raise ValueError(f"{type_name} {target!r} was not found.") from exc
        if (
            expected_updated_at
            and obj.updated_at.isoformat() != expected_updated_at
        ):
            raise ConflictError(f"{type_name.title()} {target!r} changed after it was read.")
        label = str(obj)
        cleanup = after_commit(obj) if after_commit else None
        obj.delete()
        if cleanup:
            transaction.on_commit(cleanup)
    return {
        "ok": True,
        "deleted": {"type": type_name, "target": target, "label": label},
    }


def delete_project(command, *, principal, current_slug, expected_updated_at=None):
    return _delete(
        Project,
        lookup={"slug": current_slug},
        target=current_slug,
        command=command,
        principal=principal,
        capability=Capability.DELETE_PROJECTS,
        operation="project.delete",
        type_name="project",
        expected_updated_at=expected_updated_at,
    )


def delete_asset(command, *, principal, current_slug, expected_updated_at=None):
    return _delete(
        Asset,
        lookup={"slug": current_slug},
        target=current_slug,
        command=command,
        principal=principal,
        capability=Capability.DELETE_ASSETS,
        operation="asset.delete",
        type_name="asset",
        expected_updated_at=expected_updated_at,
    )


def delete_content(command, *, principal, current_slug, expected_updated_at=None):
    return _delete(
        ContentItem,
        lookup={"slug": current_slug},
        target=current_slug,
        command=command,
        principal=principal,
        capability=Capability.DELETE_CONTENT,
        operation="content.delete",
        type_name="content",
        expected_updated_at=expected_updated_at,
    )


def delete_expense(command, *, principal, current_id, expected_updated_at=None):
    target = str(current_id)
    return _delete(
        Expense,
        lookup={"pk": current_id},
        target=target,
        command=command,
        principal=principal,
        capability=Capability.DELETE_EXPENSES,
        operation="expense.delete",
        type_name="expense",
        expected_updated_at=expected_updated_at,
    )


def delete_documentation(
    command, *, principal, current_doc_id, expected_updated_at=None
):
    return _delete(
        DocumentationRecord,
        lookup={"doc_id": current_doc_id},
        target=current_doc_id,
        command=command,
        principal=principal,
        capability=Capability.DELETE_DOCUMENTATION,
        operation="documentation.delete",
        type_name="documentation",
        expected_updated_at=expected_updated_at,
    )


def _receipt_cleanup(receipt):
    name = receipt.file.name if receipt.file else ""
    storage = receipt.file.storage if receipt.file else None
    if not name or storage is None:
        return None
    return lambda: storage.delete(name)


def delete_receipt(command, *, principal, current_id, expected_updated_at=None):
    target = str(current_id)
    return _delete(
        Receipt,
        lookup={"pk": current_id},
        target=target,
        command=command,
        principal=principal,
        capability=Capability.DELETE_RECEIPTS,
        operation="receipt.delete",
        type_name="receipt",
        expected_updated_at=expected_updated_at,
        after_commit=_receipt_cleanup,
    )
