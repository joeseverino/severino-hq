"""Canonical report exports shared by web and MCP adapters."""

from __future__ import annotations

from typing import Any

from core.audit import operation_context, record_event
from core.models import AuditLog
from reports import exports

from .security import Capability, Principal


def export_year_summary(
    year: int, output_format: str, *, principal: Principal
) -> dict[str, Any]:
    principal.require(Capability.READ)
    normalized = "md" if output_format == "markdown" else output_format
    if normalized == "md":
        body = exports.year_summary_markdown(year)
    elif normalized == "json":
        body = exports.year_summary_json(year)
    else:
        raise ValueError("format must be 'md' or 'json'")
    filename = f"year-summary-{year}.{normalized}"
    with operation_context(
        interface=principal.interface,
        actor=principal.actor,
        operation="report.export",
    ):
        record_event(
            action=AuditLog.Action.EXPORTED,
            type_label="Export",
            message=f"Generated export: {filename}",
            metadata={"filename": filename, "bytes": len(body.encode("utf-8"))},
        )
    return {"ok": True, "filename": filename, "format": normalized, "content": body}
