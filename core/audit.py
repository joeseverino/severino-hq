"""
Audit helpers.

Every domain model is registered via ``register_audit(Model, type_label)``.
post_save / post_delete signals then write to AuditLog, attributing the change
to the current request user (via CurrentUserMiddleware).
"""

from __future__ import annotations

import logging
from typing import Iterable

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .middleware import get_current_user
from .models import AuditLog


logger = logging.getLogger("severino.audit")

_AUDITED_MODELS: dict[type, str] = {}


def register_audit(model, type_label: str) -> None:
    """Register a model so create/update/delete events land in the audit log."""

    if model in _AUDITED_MODELS:
        return
    _AUDITED_MODELS[model] = type_label

    @receiver(post_save, sender=model, weak=False)
    def _on_save(sender, instance, created, **kwargs):
        record_event(
            action=AuditLog.Action.CREATED if created else AuditLog.Action.UPDATED,
            obj=instance,
            type_label=type_label,
        )

    @receiver(post_delete, sender=model, weak=False)
    def _on_delete(sender, instance, **kwargs):
        record_event(
            action=AuditLog.Action.DELETED,
            obj=instance,
            type_label=type_label,
        )


def audited_models() -> Iterable[tuple[type, str]]:
    return tuple(_AUDITED_MODELS.items())


def record_event(
    *,
    action: str,
    obj=None,
    type_label: str | None = None,
    message: str = "",
    metadata: dict | None = None,
    user=None,
) -> AuditLog:
    """Write an AuditLog row. Safe to call from views, signals, or commands."""

    user = user or get_current_user()
    if user is not None and not getattr(user, "is_authenticated", False):
        user = None

    object_type = type_label or (
        obj.__class__.__name__ if obj is not None else ""
    )
    object_id = str(getattr(obj, "pk", "")) if obj is not None else ""
    object_repr = ""
    if obj is not None:
        try:
            object_repr = str(obj)[:200]
        except Exception:  # noqa: BLE001 - defensive
            object_repr = ""

    try:
        return AuditLog.objects.create(
            user=user,
            action=action,
            object_type=object_type,
            object_id=object_id,
            object_repr=object_repr,
            message=message,
            metadata=metadata or {},
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to write AuditLog entry")
        return None  # type: ignore[return-value]
