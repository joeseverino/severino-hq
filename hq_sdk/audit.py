"""Adapter-neutral audit attribution for plugin operations."""

from core.audit import (
    audit_events,
    audit_operation,
    operation_context,
    record_event,
    record_operation,
    register_audit,
)
# Typed metadata, exported so an extension records "12 updated, 1 failed" in
# the vocabulary every other event already uses, so `metadata` stays comparable
# across callers.
from core.facets import Counts, Failure, Source, Steps, Timing
from core.models import AuditLog

AuditAction = AuditLog.Action

__all__ = [
    "AuditAction",
    "Counts",
    "Failure",
    "Source",
    "Steps",
    "Timing",
    "audit_events",
    "audit_operation",
    "operation_context",
    "record_event",
    "record_operation",
    "register_audit",
]
