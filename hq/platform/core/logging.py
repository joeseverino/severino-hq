"""Structured, dependency-free production logging primitives."""

import json
import logging
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import override

_request_id = ContextVar("severino_request_id", default="-")


def get_request_id() -> str:
    return _request_id.get()


def set_request_id(value: str) -> Token[str]:
    """Carry ``value`` as the request id for the length of a ``with`` block."""

    return _request_id.set(value)


class JsonFormatter(logging.Formatter):
    """Emit stable JSON records that remain useful in plain container logs."""

    fields = ("event", "method", "path", "status", "duration_ms", "audit")

    @override
    def format(self, record: logging.LogRecord) -> str:
        request_id = get_request_id()
        request = getattr(record, "request", None)
        if request_id == "-" and request is not None:
            request_id = getattr(request, "request_id", "-")
        payload = {
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id,
        }
        for field in self.fields:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
