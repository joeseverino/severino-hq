"""For an extension's writes: register a model's audit, and record an event or a bulk operation.

Who acted and which operation a row belongs to are bound by the door that ran
the work: the capability runner names each row by its capability, the job
runner by its job. An extension binds neither, so it has no binding to call.
"""

from hq.platform.core.audit import (
    Subject,
    audit_events,
    record_event,
    record_operation,
    register_audit,
)

# Typed metadata, exported so an extension records "12 updated, 1 failed" in
# the vocabulary every other event already uses, so `metadata` stays comparable
# across callers.
from hq.platform.core.facets import Counts, Failure, Source
from hq.platform.core.models import AuditLog

# An extension's demo writes each audited record inside one, so its event reads
# as the person it stands for made it, when they did (``PluginIntegration.demo_seed``).
from hq.platform.core.seeds import seeded_by_hand

AuditAction = AuditLog.Action


__all__ = [
    "AuditAction",
    "Counts",
    "Failure",
    "Source",
    "Subject",
    "audit_events",
    "record_event",
    "record_operation",
    "register_audit",
    "seeded_by_hand",
]
