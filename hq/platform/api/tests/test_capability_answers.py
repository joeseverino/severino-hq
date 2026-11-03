"""Each capability's answer is the shape its handler's return annotation
names, published under ``data`` of its 200; a handler that names no
shape, or one the host cannot resolve, answers an object and never breaks
the document."""

from typing import Annotated, Literal, TypedDict

from django.test import SimpleTestCase

from hq.platform.application import integration_specs
from hq.platform.application.capabilities import capability_registry
from hq.platform.application.command_fields import Describe
from hq.platform.application.integration_specs import CapabilitySpec
from hq.platform.application.integration_testing import override_integration_graph
from hq.platform.application.integrations import compile_integration_graph
from hq_sdk.capabilities import StrictCommand

from .. import openapi

_REF = "$ref"
_GENERIC = openapi.SCHEMAS + "CommandAnswer"


class ExampleCounted(TypedDict):
    """How many example things the call counted."""

    ok: Literal[True]
    counted: Annotated[int, Describe("How many example things it counted.")]


class ExampleCommand(StrictCommand):
    value: int = 0


def _typed(command: ExampleCommand, *, principal) -> ExampleCounted:
    return {"ok": True, "counted": command.value}


def _plain(command: ExampleCommand, *, principal) -> dict:
    return {"ok": True}


def _unresolvable(command: ExampleCommand, *, principal) -> ExampleMissing:  # type: ignore[name-defined]  # noqa: F821 - names a type the host cannot import, as an extension's annotation may
    return {"ok": True}


def _spec(name: str, handler, effect: str = "remote_write") -> CapabilitySpec:
    return CapabilitySpec(name, "Exercise an example answer.", effect, "example.write", ExampleCommand, handler)


def _data(document: dict, name: str) -> dict:
    """The schema of ``data`` in the 200 answer of the capability ``name``, its references followed."""

    (operation,) = (
        operation
        for methods in document["paths"].values()
        for operation in methods.values()
        if operation.get("x-hq-capability") == name
    )
    success = _resolved(document, operation["responses"]["200"])
    return _resolved(document, success["content"]["application/json"]["schema"])["properties"]["data"]


def _resolved(document: dict, value: dict) -> dict:
    while _REF in value:
        _, section, kind, name = value[_REF].split("/")
        value = document[section][kind][name]
    return value


class CapabilityAnswerTests(SimpleTestCase):
    def test_every_host_capability_names_the_shape_it_answers(self):
        host = [spec for spec in capability_registry().values() if spec.handler.__module__.startswith("hq.")]

        self.assertTrue(host)
        self.assertEqual(
            [spec.name for spec in host if integration_specs.answer_type(spec) is None],
            [],
            "Annotate the handler's return with the TypedDict it answers, beside the handler; "
            "a refusal it returns is `| RefusalEnvelope`.",
        )

    def test_every_host_capability_in_the_document_answers_its_own_shape(self):
        document = openapi.document()
        names = [
            spec.name
            for spec in capability_registry().values()
            if spec.handler.__module__.startswith("hq.")
            and any(
                operation.get("x-hq-capability") == spec.name
                for methods in document["paths"].values()
                for operation in methods.values()
            )
        ]

        self.assertTrue(names)
        self.assertEqual([name for name in names if _data(document, name).get(_REF) == _GENERIC], [])

    def test_an_answer_is_its_type_less_ok_whether_it_changes_something_or_reads(self):
        specs = (_spec("example.counted", _typed), _spec("example.counted_read", _typed, "read"))
        with override_integration_graph(compile_integration_graph(capabilities=specs, resources=(), connections=())):
            document = openapi.document()

        for name in ("example.counted", "example.counted_read"):
            with self.subTest(name):
                self.assertEqual(sorted(_resolved(document, _data(document, name))["properties"]), ["counted"])

    def test_a_handler_naming_no_shape_or_one_the_host_cannot_resolve_answers_an_object(self):
        specs = (_spec("example.plain", _plain), _spec("example.unresolvable", _unresolvable))
        with override_integration_graph(compile_integration_graph(capabilities=specs, resources=(), connections=())):
            document = openapi.document()

        for name in ("example.plain", "example.unresolvable"):
            with self.subTest(name):
                self.assertEqual(_data(document, name), {_REF: _GENERIC})
