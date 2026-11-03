"""No log line carries a value a caller sent inside a validation failure, whichever logger writes it.

A payload can hold a secret (a certificate's private key), and a log travels
further than the request did. Pydantic's own text quotes the input
(``input_value=``), so the formatter every line passes through tells a
validation failure by where and how it failed, never by what it held.
"""

import json
import logging
from typing import Never

from django.test import SimpleTestCase
from pydantic import BaseModel, ValidationError

from hq.platform.application import refusal_status
from hq.platform.core.logging import JsonFormatter

PLANTED = "-----BEGIN PRIVATE KEY----- example-planted-7f3a"
LOGGER = "example.worker"


class ExampleKeyCommand(BaseModel):
    private_key: int


def _invalid() -> ValidationError:
    try:
        ExampleKeyCommand(private_key=PLANTED)
    except ValidationError as exc:
        return exc
    raise AssertionError("the command accepted its planted value")


def _raise(kind: type[Exception], cause: BaseException | None) -> Never:
    if cause is None:
        raise kind("The handler could not read its command.")
    raise kind("The handler could not read its command.") from cause


def _under(kind: type[Exception], invalid: ValidationError | None, *, implicit: bool = False) -> Exception:
    """A handler's failure raised over ``invalid``, as its cause or, raised while handling it, its context;
    over nothing, without one."""

    try:
        if invalid is None:
            _raise(kind, None)
        else:
            try:
                raise invalid
            except ValidationError as handled:
                _raise(kind, None if implicit else handled)
    except kind as exc:
        return exc
    raise AssertionError("nothing was raised")


def _failures() -> dict[str, Exception]:
    failures: dict[str, Exception] = {"raised": _invalid()}
    for kind in (RuntimeError, ValueError):
        failures[f"{kind.__name__} from it"] = _under(kind, _invalid())
        failures[f"{kind.__name__} while handling it"] = _under(kind, _invalid(), implicit=True)
    return failures


class ValidationFailureLogTests(SimpleTestCase):
    def lines(self, logger: str, write) -> list[str]:
        """Each line ``write`` leaves on ``logger``, as the production formatter writes it."""

        kept: list[logging.LogRecord] = []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = kept.append  # type: ignore[method-assign]  # a handler that keeps what it is handed
        target = logging.getLogger(logger)
        old_level, old_propagate = target.level, target.propagate
        target.addHandler(handler)
        target.setLevel(logging.DEBUG)
        target.propagate = False
        try:
            write()
        finally:
            target.removeHandler(handler)
            target.setLevel(old_level)
            target.propagate = old_propagate
        return [JsonFormatter().format(record) for record in kept]

    def test_a_failure_logged_by_any_logger_carries_no_value_the_caller_sent(self):
        for case, exc in _failures().items():
            with self.subTest(case):
                lines = self.lines(LOGGER, lambda exc=exc: logging.getLogger(LOGGER).error("failed", exc_info=exc))

                self.assertTrue(lines)
                self.assertFalse([line for line in lines if PLANTED in line], lines)

    def test_a_failed_capability_logs_no_value_the_caller_sent(self):
        for case, exc in _failures().items():
            with self.subTest(case):
                lines = self.lines("severino", lambda exc=exc: refusal_status._mapped(exc, "example.records.change"))

                self.assertFalse([line for line in lines if PLANTED in line], lines)

    def test_a_failure_given_as_a_message_argument_carries_no_value_the_caller_sent(self):
        invalid = _invalid()
        for case, (message, args) in {
            "positional": ("Could not read: %s", (invalid,)),
            "named": ("Could not read: %(error)s", ({"error": invalid},)),
        }.items():
            with self.subTest(case):
                (line,) = self.lines(LOGGER, lambda m=message, a=args: logging.getLogger(LOGGER).warning(m, *a))

                self.assertNotIn(PLANTED, line)
                self.assertIn("private_key", json.loads(line)["message"])

    def test_a_told_failure_still_names_each_link_and_where_it_failed(self):
        exc = _under(RuntimeError, _invalid())

        (line,) = self.lines(LOGGER, lambda: logging.getLogger(LOGGER).error("failed", exc_info=exc))
        traceback = json.loads(line)["exception"]

        self.assertIn(f"{ValidationError.__module__}.{ValidationError.__qualname__}:", traceback)
        self.assertIn("RuntimeError: The handler could not read its command.", traceback)
        self.assertIn("private_key [int_parsing]", traceback)

    def test_a_failure_with_no_validation_in_its_chain_is_formatted_as_it_is(self):
        failure = _under(LookupError, None)

        (line,) = self.lines(LOGGER, lambda: logging.getLogger(LOGGER).error("failed", exc_info=failure))

        self.assertEqual(
            json.loads(line)["exception"],
            logging.Formatter().formatException((LookupError, failure, failure.__traceback__)),
        )
