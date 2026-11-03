"""Structured production logging primitives."""

import json
import logging
import sys
from collections.abc import Mapping
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from functools import cache
from types import TracebackType
from typing import TypeIs, override

lazy from pydantic import ValidationError as PydanticValidationError

_request_id = ContextVar("severino_request_id", default="-")


def get_request_id() -> str:
    return _request_id.get()


def set_request_id(value: str) -> Token[str]:
    """Carry ``value`` as the request id for the length of a ``with`` block."""

    return _request_id.set(value)


def _invalid(value: object) -> TypeIs[PydanticValidationError]:
    """Whether ``value`` is a validation failure: one exists only once pydantic_core is loaded, and a
    logged line never loads it."""

    return "pydantic_core" in sys.modules and isinstance(value, PydanticValidationError)


def _validation_summary(exc: PydanticValidationError) -> str:
    """A validation failure by where and how it failed: Pydantic's own text quotes the input."""

    where = "; ".join(f"{'.'.join(map(str, error['loc'])) or '(input)'} [{error['type']}]" for error in exc.errors())
    return f"{exc.title} failed validation at {where}"


@cache
def _stand_in(kind: type[BaseException]) -> type[Exception]:
    """An exception class a traceback names as ``kind``, to carry a link's safe text in its place."""

    return type(kind.__name__, (Exception,), {"__module__": kind.__module__, "__qualname__": kind.__qualname__})


def _chain(exc: BaseException) -> list[BaseException]:
    links: list[BaseException] = []
    link: BaseException | None = exc
    while link is not None and link not in links:
        links.append(link)
        link = link.__cause__ or (None if link.__suppress_context__ else link.__context__)
    return links


def _logged_failure(exc: BaseException) -> BaseException:
    """``exc`` as a traceback in the log carries it, with each validation failure in its chain told by
    ``_validation_summary``; ``exc`` itself when its chain holds none, and is never changed."""

    links = _chain(exc)
    if not any(map(_invalid, links)):
        return exc
    told: BaseException | None = None
    for link in reversed(links):
        text = _validation_summary(link) if _invalid(link) else str(link)
        stand_in = _stand_in(type(link))(text).with_traceback(link.__traceback__)
        if link.__cause__ is not None:
            stand_in.__cause__ = told
        else:
            stand_in.__context__ = told
        told = stand_in
    return told or exc


type _ExcInfo = tuple[type[BaseException], BaseException, TracebackType | None] | tuple[None, None, None]


class JsonFormatter(logging.Formatter):
    """Emit stable JSON records that remain useful in plain container logs.

    Every line HQ logs passes through it, so it is where a validation failure
    is told by where and how it failed: Pydantic's text quotes the value the
    caller sent, which can be a secret, in a message argument or anywhere in a
    traceback's chain.
    """

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
            "message": self._message(record),
            "request_id": request_id,
        }
        for field in self.fields:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    @override
    def formatException(self, ei: _ExcInfo) -> str:
        exc = ei[1]
        if exc is None:
            return super().formatException(ei)
        told = _logged_failure(exc)
        return super().formatException((type(told), told, told.__traceback__))

    @staticmethod
    def _message(record: logging.LogRecord) -> str:
        args = record.args
        values = args.values() if isinstance(args, Mapping) else args or ()
        if not any(map(_invalid, values)):
            return record.getMessage()
        if isinstance(args, Mapping):
            told: object = {key: _validation_summary(v) if _invalid(v) else v for key, v in args.items()}
        else:
            told = tuple(_validation_summary(v) if _invalid(v) else v for v in values)
        return str(record.msg) % told
