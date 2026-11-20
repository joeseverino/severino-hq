import importlib
import inspect
import pkgutil
from annotationlib import Format
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

import hq_sdk
from hq.platform.application.capabilities import CapabilitySpec
from hq.platform.application.integration_testing import commands_registered
from hq.platform.application.security import Principal
from hq.platform.core.models import AuditLog
from hq_sdk.audit import record_operation
from hq_sdk.capabilities import execute_capability


@dataclass(frozen=True, slots=True)
class _Nothing:
    pass


def _exports(module):
    """What a module offers an extension: its ``__all__``, or the public names it defines itself."""

    declared = getattr(module, "__all__", None)
    if declared is not None:
        return declared
    return [
        name
        for name, value in vars(module).items()
        if not name.startswith("_") and getattr(value, "__module__", None) == module.__name__
    ]


def _routines(name, value):
    """An export that is a routine, or each public function of an export that is a class
    other than an exception, by its qualified name: what an extension calls."""

    if inspect.isroutine(value):
        yield name, value
    elif inspect.isclass(value) and not issubclass(value, BaseException):
        for member, function in vars(value).items():
            if not member.startswith("_") and inspect.isfunction(function):
                yield f"{name}.{member}", function


def _untyped(name, routine):
    """Each parameter of ``routine`` but ``self`` or ``cls``, and its return, that carries no annotation."""

    try:
        signature = inspect.signature(routine, annotation_format=Format.STRING)
    except TypeError, ValueError:
        return []
    gaps = [
        f"{name}({parameter.name})"
        for parameter in signature.parameters.values()
        if parameter.name not in ("self", "cls") and parameter.annotation is inspect.Parameter.empty
    ]
    if signature.return_annotation is inspect.Signature.empty:
        gaps.append(f"{name} -> ?")
    return gaps


class SdkContractTests(SimpleTestCase):
    def test_every_export_is_fully_typed(self):
        """An extension type-checks its calls into the SDK. An unannotated
        parameter or return there is a ``type: ignore`` in every extension that
        calls it, so the whole surface is typed or the suite fails."""

        gaps = []
        for info in pkgutil.iter_modules(hq_sdk.__path__):
            module = importlib.import_module(f"hq_sdk.{info.name}")
            for export in _exports(module):
                for qualified, routine in _routines(f"{info.name}.{export}", getattr(module, export)):
                    gaps += _untyped(qualified, routine)
        self.assertEqual(gaps, [])

    def test_style_validation_reads_the_host_bundle_after_package_moves(self):
        from hq_sdk.testing import undefined_style_classes

        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "example.html").write_text(
                '<div class="hq-sdk-intentionally-undefined-class"></div>', encoding="utf-8"
            )
            self.assertEqual(
                undefined_style_classes(root),
                ["example.html: .hq-sdk-intentionally-undefined-class"],
            )

    def test_a_command_with_a_pydantic_config_of_its_own_is_refused_where_it_is_declared(self):
        """Such a config holds over the one HQ closes a command with, so even
        one closed today is a command a later edit of its config opens."""

        from typing import TypedDict

        from pydantic import BaseModel, ConfigDict, with_config

        class ExampleModel(BaseModel):
            value: int

        class ExampleClosedModel(BaseModel):
            model_config = ConfigDict(extra="forbid", frozen=True)
            value: int

        @with_config(ConfigDict(extra="allow"))
        class ExampleOpen(TypedDict):
            value: int

        @with_config(ConfigDict(extra="forbid"))
        class ExampleClosed(TypedDict):
            value: int

        for command in (ExampleModel, ExampleClosedModel, ExampleOpen, ExampleClosed):
            with self.subTest(command.__name__), self.assertRaises(TypeError):
                CapabilitySpec("example.open", "Open it.", "remote_write", "example.write", command, dict)

    def test_a_command_declares_its_fields_with_the_hosts_markers(self):
        from typing import Annotated

        from hq.domains.calendars.models import Entry
        from hq.platform.application.integration_specs import command_schema
        from hq_sdk.capabilities import Choices, Describe, Names, Refers

        @dataclass(frozen=True, slots=True)
        class ExampleCommand:
            note: Annotated[str, Describe("What to write down.")]
            project: Annotated[str, Names("example.projects", key="slug")]
            tone: Annotated[str, Choices((("plain", "Plain"),))]
            about: Annotated[str, Refers(Entry, "about")]

        fields = command_schema(ExampleCommand)["properties"]

        self.assertEqual(fields["note"]["description"], "What to write down.")
        self.assertEqual(fields["project"]["x-hq-records"], {"resource": "example.projects", "key": "slug"})
        self.assertEqual(fields["tone"]["enum"], ["plain"])
        self.assertEqual(fields["tone"]["x-enumDescriptions"], {"plain": "Plain"})
        self.assertEqual(fields["about"]["x-hq-reference"], {"model": "calendars.entry", "field": "about"})


class AuditSdkTests(TestCase):
    def test_a_handler_s_bulk_row_is_its_capability_s_and_its_caller_s(self):
        def handler(command, *, principal):
            record_operation("example.rows", "Imported records.", metadata={"changed": 2})
            return {"ok": True}

        spec = CapabilitySpec("example.import", "Import examples.", "remote_write", "example.write", _Nothing, handler)
        principal = Principal("example-client", "cli", frozenset({"example.write"}))

        with commands_registered(spec):
            self.assertTrue(execute_capability(spec.name, {}, principal=principal)["ok"])

        event = AuditLog.objects.get(object_type="example.rows")
        self.assertEqual(event.action, AuditLog.Action.UPDATED)
        self.assertEqual(
            event.metadata,
            {"actor": "example-client", "changed": 2, "interface": "cli", "operation": "example.import"},
        )

    def test_required_audit_event_fails_closed(self):
        with (
            patch.object(AuditLog.objects, "create", side_effect=RuntimeError("db")),
            self.assertRaises(RuntimeError),
        ):
            record_operation("example.import", "Imported.", required=True)

    def test_best_effort_audit_event_preserves_signal_safety(self):
        with patch.object(AuditLog.objects, "create", side_effect=RuntimeError("db")):
            event = record_operation("example.refresh", "Refreshed.")
        self.assertIsNone(event)
