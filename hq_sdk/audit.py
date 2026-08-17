"""Adapter-neutral audit attribution for plugin operations."""

from core.audit import (
    audit_events,
    audit_operation,
    operation_context,
    record_event,
    record_operation,
    register_audit,
)
from core.models import AuditLog

AuditAction = AuditLog.Action

__all__ = [
    "AuditAction",
    "audit_events",
    "audit_operation",
    "operation_context",
    "record_event",
    "record_operation",
    "register_audit",
]
