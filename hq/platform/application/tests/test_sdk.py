from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.views import View
from pydantic import ValidationError

from hq.platform.application.capabilities import CapabilitySpec
from hq.platform.application.connections import ConnectionSpec
from hq.platform.application.plugins import NavigationItem, PluginManifest
from hq.platform.application.security import Principal
from hq.platform.application.workflows import WorkflowPlan
from hq.platform.core.models import AuditLog
from hq_sdk.audit import audit_operation, record_operation
from hq_sdk.capabilities import StrictCommand
from hq_sdk.plugin import NavigationItem as SdkNavigationItem, PluginManifest as SdkPluginManifest
from hq_sdk.validation import unsupported_hq_imports
from hq_sdk.web import CapabilityRequiredMixin


class SdkContractTests(SimpleTestCase):
    def test_every_export_is_fully_typed(self):
        """An extension type-checks its calls into the SDK. An unannotated
        parameter or return there is a ``type: ignore`` in every extension that
        calls it, so the whole surface is typed or the suite fails."""

        import importlib
        import inspect
        from annotationlib import Format

        from hq_sdk.contract import exports, module_names

        def untyped(name, routine):
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

        gaps = []
        for module_name in module_names():
            module = importlib.import_module(f"hq_sdk.{module_name}")
            for export in exports(module):
                value = getattr(module, export)
                qualified = f"{module_name}.{export}"
                if inspect.isroutine(value):
                    gaps += untyped(qualified, value)
                elif inspect.isclass(value) and not issubclass(value, BaseException):
                    for member, function in vars(value).items():
                        if not member.startswith("_") and inspect.isfunction(function):
                            gaps += untyped(f"{qualified}.{member}", function)
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

    def test_strict_commands_reject_unknown_input(self):
        class ExampleCommand(StrictCommand):
            value: int

        with self.assertRaises(ValidationError):
            ExampleCommand.model_validate({"value": 1, "typo": True})

    def test_plugin_source_can_use_only_the_sdk(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "valid.py").write_text(
                "from hq_sdk.capabilities import CapabilitySpec\n",
                encoding="utf-8",
            )
            self.assertEqual(unsupported_hq_imports(root), [])

    def test_the_installed_sdk_rejects_the_hq_namespace_without_host_discovery(self):
        from hq_sdk import validation

        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "plugin.py").write_text(
                "from hq.platform import application\n"
                "from hq import platform\n"
                "import hq.domains.projects\n"
                "from hq_sdk import ui\n",
                encoding="utf-8",
            )
            with patch("hq_sdk.validation.HOST_INTERNAL_PACKAGES", validation._FLOOR):
                self.assertEqual(
                    unsupported_hq_imports(root),
                    ["plugin.py:1: hq.platform", "plugin.py:2: hq", "plugin.py:3: hq.domains.projects"],
                )

    def test_plugin_source_reports_host_internal_imports(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "invalid.py").write_text(
                "from hq.platform.application.capabilities import CapabilitySpec\nimport hq.domains.core\n",
                encoding="utf-8",
            )
            self.assertEqual(
                unsupported_hq_imports(root),
                ["invalid.py:1: hq.platform.application.capabilities", "invalid.py:2: hq.domains.core"],
            )

    def test_a_plugin_package_named_like_a_host_app_is_its_own(self):
        from hq_sdk import validation

        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "plugin.py").write_text(
                "from core import audit\nimport jobs.runner\nfrom calendars.models import Entry\n",
                encoding="utf-8",
            )
            with patch("hq_sdk.validation.HOST_INTERNAL_PACKAGES", validation._FLOOR):
                self.assertEqual(unsupported_hq_imports(root), [])

    def test_the_host_packages_are_the_packages_of_its_tree_and_nothing_else(self):
        from hq_sdk import validation

        root = Path(validation.__file__).resolve().parents[1]
        top_level = {
            entry.name
            for entry in root.iterdir()
            if entry.is_dir() and (entry / "__init__.py").exists() and entry.name != "hq_sdk"
        }
        self.assertEqual(validation.HOST_INTERNAL_PACKAGES, top_level)

    def test_a_relative_import_is_the_plugins_own_whatever_it_is_called(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "plugin.py").write_text(
                "from .calendars import sources\nfrom ..core import helpers\nfrom hq.domains.calendars import Entry\n",
                encoding="utf-8",
            )
            self.assertEqual(unsupported_hq_imports(root), ["plugin.py:3: hq.domains.calendars"])

    def test_the_import_boundary_reads_every_form_and_every_file(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pkg").mkdir()
            (root / "pkg" / "deep.py").write_text(
                "import os, hq.jobs.runner\n"
                "from hq_sdk import ui\n"
                "from . import sibling\n"
                "def f():\n"
                "    from hq import audit; from hq import audit\n",
                encoding="utf-8",
            )
            (root / "broken.py").write_text("def (:\n", encoding="utf-8")

            found = unsupported_hq_imports(root)

        self.assertEqual(found[1:], ["pkg/deep.py:1: hq.jobs.runner", "pkg/deep.py:5: hq"])
        self.assertTrue(found[0].startswith("broken.py:1: "))

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


class _AllowedView(CapabilityRequiredMixin, View):
    required_capability = "example.read"

    def get(self, request):
        return HttpResponse("ok")


class _UnconfiguredView(CapabilityRequiredMixin, View):
    def get(self, request):
        return HttpResponse("unreachable")


class WebSdkTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = SimpleNamespace(is_authenticated=True)

    def test_capability_mixin_allows_a_narrow_authorized_principal(self):
        request = self.factory.get("/example/")
        request.user = self.user
        principal = Principal("operator", "web", frozenset({"example.read"}))
        with patch("hq_sdk.web.web_principal", return_value=principal):
            response = _AllowedView.as_view()(request)
        self.assertEqual(response.status_code, 200)

    def test_capability_mixin_converts_denial_to_django_permission_denied(self):
        request = self.factory.get("/example/")
        request.user = self.user
        principal = Principal("operator", "web", frozenset())
        with patch("hq_sdk.web.web_principal", return_value=principal), self.assertRaises(PermissionDenied):
            _AllowedView.as_view()(request)

    def test_capability_mixin_requires_an_explicit_capability(self):
        request = self.factory.get("/example/")
        request.user = self.user
        with self.assertRaises(ImproperlyConfigured):
            _UnconfiguredView.as_view()(request)


class AuditSdkTests(TestCase):
    def test_operation_attribution_is_shared(self):
        principal = Principal("example-client", "api", frozenset())
        with audit_operation(
            operation="example.import",
            principal=principal,
            operation_id="operation-123",
        ):
            event = record_operation("example.import", "Imported records.", metadata={"changed": 2})
        event.refresh_from_db()
        self.assertEqual(event.action, AuditLog.Action.UPDATED)
        self.assertEqual(event.operation_id, "operation-123")
        self.assertEqual(
            event.metadata,
            {
                "actor": "example-client",
                "changed": 2,
                "interface": "api",
                "operation": "example.import",
            },
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
                    "hq_sdk changed shape. Every extension binds to it, and no test "
                    "or graph here can see them. Run `mise run regen`, "
                    "review the diff, and decide whether PLUGIN_API_VERSION moves:",
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
        del current["modules"]["web"]["safe_next"]
        current["modules"]["ui"]["Brand"] = {"kind": "value", "type": "str"}

        differences = drift(committed, current)

        self.assertEqual(len(differences), 3)
        self.assertTrue(differences[0].startswith("~ hq_sdk.capabilities.execute_capability: parameters:"))
        self.assertEqual(differences[1], "+ hq_sdk.ui.Brand")
        self.assertEqual(differences[2], "- hq_sdk.web.safe_next")

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
