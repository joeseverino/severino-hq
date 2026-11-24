"""HQ's log line: one JSON object per record."""

import json
import logging
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from functools import cache
from types import TracebackType
from typing import TypeIs, override

lazy from pydantic import ValidationError as PydanticValidationError

from .tracing import NO_TRACE, trace_id


def _invalid(value: object) -> TypeIs[PydanticValidationError]:
    """Whether ``value`` is a validation failure: one exists only once pydantic_core is loaded, and a
    logged line never loads it."""

    return "pydantic_core" in sys.modules and isinstance(value, PydanticValidationError)


def _validation_summary(exc: PydanticValidationError) -> str:
    """A validation failure by where and how it failed: Pydantic's own text quotes the input."""

    where = "; ".join(f"{'.'.join(map(str, error['loc'])) or '(input)'} [{error['type']}]" for error in exc.errors())
    return f"{exc.title} failed validation at {where}"


def _told(value: object) -> object:
    return _validation_summary(value) if _invalid(value) else value


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


def logged_failure(exc: BaseException) -> BaseException:
    """``exc`` as a traceback in the log carries it, with each validation failure in its chain told by
    ``_validation_summary``; ``exc`` itself when its chain holds none, and is never changed."""

    links = _chain(exc)
    if not any(map(_invalid, links)):
        return exc
    told: BaseException | None = None
    for link in reversed(links):
        stand_in = _stand_in(type(link))(str(_told(link))).with_traceback(link.__traceback__)
        if link.__cause__ is not None:
            stand_in.__cause__ = told
        else:
            stand_in.__context__ = told
        told = stand_in
    return told or exc


type _ExcInfo = tuple[type[BaseException], BaseException, TracebackType | None] | tuple[None, None, None]


def request_id(record: logging.LogRecord) -> str:
    """The id of the request ``record`` was logged in, or ``NO_TRACE``."""

    # Django logs a response a view returned once every middleware has
    # finished with it, past the trace, and hands the request beside it.
    traced = trace_id()
    return traced if traced != NO_TRACE else getattr(getattr(record, "request", None), "request_id", NO_TRACE)


class LineFormatter(logging.Formatter):
    """Emit stable JSON records that remain useful in plain container logs.

    Every line HQ logs passes through it, so it is where a validation failure
    is told by where and how it failed: Pydantic's text quotes the value the
    caller sent, which can be a secret, in a message argument or anywhere in a
    traceback's chain.
    """

    fields = ("event", "method", "path", "status", "duration_ms", "audit")

    @override
    def format(self, record: logging.LogRecord) -> str:
        line = {
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": self._message(record),
            "request_id": request_id(record),
        } | {field: value for field in self.fields if (value := getattr(record, field, None)) is not None}
        if record.exc_info:
            line["exception"] = self.formatException(record.exc_info)
        return json.dumps(line, separators=(",", ":"), ensure_ascii=False)

    @override
    def formatException(self, ei: _ExcInfo) -> str:
        told = ei if ei[1] is None else (type(exc := logged_failure(ei[1])), exc, exc.__traceback__)
        return super().formatException(told)

    @staticmethod
    def _message(record: logging.LogRecord) -> str:
        args = record.args
        told = (
            {key: _told(v) for key, v in args.items()} if isinstance(args, Mapping) else tuple(map(_told, args or ()))
        )
        return str(record.msg) % told if told else str(record.msg)
