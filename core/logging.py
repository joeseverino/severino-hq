"""Structured, dependency-free production logging primitives."""

from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging


_request_id = ContextVar("severino_request_id", default="-")


def get_request_id() -> str:
    return _request_id.get()


def set_request_id(value: str):
    return _request_id.set(value)


def reset_request_id(token) -> None:
    _request_id.reset(token)


class JsonFormatter(logging.Formatter):
    """Emit stable JSON records that remain useful in plain container logs."""

    fields = ("event", "method", "path", "status", "duration_ms")

    def format(self, record: logging.LogRecord) -> str:
        request_id = get_request_id()
        request = getattr(record, "request", None)
        if request_id == "-" and request is not None:
            request_id = getattr(request, "request_id", "-")
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
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
