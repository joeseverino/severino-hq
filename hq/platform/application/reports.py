"""Canonical report exports shared by web and MCP adapters."""

from typing import Literal, TypedDict

from hq.domains.reports import exports
from hq.platform.core.audit import audit_operation, record_event
from hq.platform.core.models import AuditLog

from .security import Capability, Principal
from .sensitivity import safe_records

# What a year summary is exported as: Markdown or JSON, by the file's own extension.
ExportFormat = Literal["md", "json"]


class YearSummary(TypedDict):
    """One year's summary as the file it is exported as."""

    filename: str
    format: ExportFormat
    content: str


def export_year_summary(year: int, output_format: ExportFormat, *, principal: Principal) -> YearSummary:
    principal.require(Capability.READ)
    render = exports.year_summary_markdown if output_format == "md" else exports.year_summary_json
    body = render(year, documentation=safe_records())
    filename = f"year-summary-{year}.{output_format}"
    with audit_operation(operation="report.export", principal=principal):
        record_event(
            action=AuditLog.Action.EXPORTED,
            type_label="Export",
            message=f"Generated export: {filename}",
            metadata={"filename": filename, "bytes": len(body.encode("utf-8"))},
        )
    return {"filename": filename, "format": output_format, "content": body}
