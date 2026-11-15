import inspect
from annotationlib import Format
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from hq.platform.application.capabilities import CapabilitySpec
from hq.platform.application.connections import ConnectionSpec
from hq.platform.application.integration_testing import commands_registered
from hq.platform.application.plugins import NavigationItem, PluginManifest
from hq.platform.application.security import Principal
from hq.platform.application.workflows import WorkflowPlan
from hq.platform.core.models import AuditLog
from hq_sdk.audit import record_operation
from hq_sdk.capabilities import execute_capability
from hq_sdk.plugin import NavigationItem as SdkNavigationItem, PluginManifest as SdkPluginManifest


@dataclass(frozen=True, slots=True)
class _Nothing:
    pass


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

        import importlib

        from hq_sdk.contract import exports, module_names

        gaps = []
        for module_name in module_names():
            module = importlib.import_module(f"hq_sdk.{module_name}")
            for export in exports(module):
                for qualified, routine in _routines(f"{module_name}.{export}", getattr(module, export)):
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

    def test_manifest_contract_is_the_supported_facade(self):
        self.assertIs(SdkNavigationItem, NavigationItem)
        self.assertIs(SdkPluginManifest, PluginManifest)

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
                CapabilitySpec("example.open", "Open it.", "local_write", "example.write", command, dict)

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

    def test_capability_spec_is_available_from_the_sdk(self):
        from hq_sdk.capabilities import CapabilitySpec as SdkCapabilitySpec

        self.assertIs(SdkCapabilitySpec, CapabilitySpec)

    def test_connection_spec_is_available_from_the_sdk(self):
        from hq.platform.application.connections import describe_connections
        from hq_sdk.connections import (
            ConnectionSpec as SdkConnectionSpec,
            describe_connections as sdk_describe_connections,
        )

        self.assertIs(SdkConnectionSpec, ConnectionSpec)
        self.assertIs(sdk_describe_connections, describe_connections)

    def test_resolution_workflows_are_available_to_every_plugin_domain(self):
        from hq_sdk.workflows import WorkflowPlan as SdkWorkflowPlan

        self.assertIs(SdkWorkflowPlan, WorkflowPlan)


class AuditSdkTests(TestCase):
    def test_a_handler_s_bulk_row_is_its_capability_s_and_its_caller_s(self):
        def handler(command, *, principal):
            record_operation("example.rows", "Imported records.", metadata={"changed": 2})
            return {"ok": True}

        spec = CapabilitySpec("example.import", "Import examples.", "local_write", "example.write", _Nothing, handler)
        principal = Principal("example-client", "cli", frozenset({"example.write"}))

        with commands_registered({spec.name: spec}):
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


class SdkShapeTests(SimpleTestCase):
    """The committed contract is the surface extensions are built against."""

    def test_the_exports_still_have_the_committed_shape(self):
        from hq_sdk.contract import describe, drift, load_committed

        differences = drift(load_committed(), describe())

        self.assertEqual(
            differences,
            [],
            "\n".join(
                [
                    (
                        "hq_sdk changed shape. Every extension binds to it, and no test "
                        "or graph here can see them. Run `mise run fix`, "
                        "review the diff, and decide whether PLUGIN_API_VERSION moves:"
                    ),
                    *differences,
                ]
            ),
        )

    def test_every_sdk_module_and_export_is_described(self):
        import importlib

        from hq_sdk.contract import describe, exports, module_names

        contract = describe()

        self.assertEqual(tuple(sorted(contract["modules"])), module_names())
        for name in module_names():
            module = importlib.import_module(f"hq_sdk.{name}")
            self.assertEqual(tuple(sorted(contract["modules"][name])), tuple(sorted(exports(module))))

    def test_a_shape_change_is_named_precisely(self):
        import copy

        from hq_sdk.contract import describe, drift

        committed = describe()
        current = copy.deepcopy(committed)
        current["modules"]["capabilities"]["execute_capability"]["parameters"].append("surprise")
        del current["modules"]["web"]["capability_required"]
        current["modules"]["ui"]["Brand"] = {"kind": "value", "type": "str"}

        differences = drift(committed, current)

        self.assertEqual(len(differences), 3)
        self.assertTrue(differences[0].startswith("~ hq_sdk.capabilities.execute_capability: parameters:"))
        self.assertEqual(differences[1], "+ hq_sdk.ui.Brand")
        self.assertEqual(differences[2], "- hq_sdk.web.capability_required")

    def test_a_reshaped_export_names_each_member_gained_lost_and_changed(self):
        from hq_sdk.contract import drift

        before = {"kind": "class", "bases": ["A"], "members": {"a": 1, "b": 2, "c": 3, "d": 4}}
        after = {"kind": "class", "bases": ["B"], "members": {"a": 1, "b": 9, "e": 5, "f": 6}}

        self.assertEqual(
            drift({"modules": {"kept": {"Thing": before}}}, {"modules": {"kept": {"Thing": after}}}),
            ['~ hq_sdk.kept.Thing: bases: ["A"] -> ["B"]; members: +e,f, -c,d, ~b'],
        )
        self.assertEqual(
            drift(
                {"modules": {"kept": {"Thing": before}}}, {"modules": {"kept": {"Thing": {**before, "members": {}}}}}
            ),
            ["~ hq_sdk.kept.Thing: members: -a,b,c,d"],
        )

    def test_versions_and_whole_modules_are_named_too(self):
        from hq_sdk.contract import drift

        committed = {
            "api_version": 1,
            "sdk_version": "1.0",
            "modules": {"gone": {"a": {}}, "kept": {"x": {"kind": "value"}}},
        }
        current = {
            "api_version": 2,
            "sdk_version": "1.0",
            "modules": {"kept": {"x": {"kind": "value"}}, "new": {"b": {}, "a": {}}},
        }

        self.assertEqual(
            drift(committed, current),
            ["api_version: 1 -> 2", "- hq_sdk.gone", "+ hq_sdk.new: a, b"],
        )
        self.assertEqual(drift(committed, committed), [])

    def test_parameters_are_recorded_without_annotations(self):
        """Annotations render differently across interpreters; names and defaults do not."""

        from hq_sdk.contract import describe

        recorded = describe()["modules"]["capabilities"]["execute_capability"]

        self.assertEqual(recorded["kind"], "function")
        self.assertIn("name", recorded["parameters"])
        self.assertTrue(all(":" not in item for item in recorded["parameters"]))
