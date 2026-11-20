"""Canonical report exports shared by web and MCP adapters."""

from typing import Literal, TypedDict

from hq.domains.reports import exports
from hq.platform.core import bindings
from hq.platform.core.audit import record_event
from hq.platform.core.models import AuditLog

from .security import Principal
from .sensitivity import safe_records
from .titled import Titled

# Taking an export away is a read the web and MCP serve outside the runner.
EXPORT = Titled("report.export", "Exported a report")

# What a year summary is exported as: Markdown or JSON, by the file's own extension.
ExportFormat = Literal["md", "json"]


class YearSummary(TypedDict):
    """One year's summary as the file it is exported as."""

    filename: str
    format: ExportFormat
    content: str


def export_year_summary(year: int, output_format: ExportFormat, *, principal: Principal) -> YearSummary:
    render = exports.year_summary_markdown if output_format == "md" else exports.year_summary_json
    body = render(year, documentation=safe_records())
    filename = f"year-summary-{year}.{output_format}"
    exported(filename, body, principal=principal)
    return {"filename": filename, "format": output_format, "content": body}


def exported(filename: str, body: str, *, principal: Principal) -> None:
    """Record that ``principal`` took ``filename`` away: the one record of an
    export, whichever door handed it over."""

    with bindings.audit_operation(operation=EXPORT, principal=principal):
        record_event(
            action=AuditLog.Action.EXPORTED,
            type_label="Export",
            message=f"Generated export: {filename}",
            metadata={"filename": filename, "bytes": len(body.encode("utf-8"))},
        )
