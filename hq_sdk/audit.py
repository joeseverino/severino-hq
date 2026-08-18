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
# the vocabulary every other event already uses. Without these an extension can
# only pass a free-form dict, which is how `metadata` became uncomparable
# across callers in the first place -- the problem facets were added to solve.
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
