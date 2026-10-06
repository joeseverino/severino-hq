"""What the registry declares reaches the controller's contracts by derivation.

Each fact has one owner. The registry's are built into schemas every time
(``bridge_registry``), the bridge's own are written in ``bridge_base.json``,
and the files the controller's generator and the secret renderer read are
written from the two. These hold the join, the files, and the names the
controller's constants take.
"""

from __future__ import annotations

import ast
import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from hq.domains.control_plane import bridge_contract, bridge_registry
from hq.domains.control_plane.connection_kinds import CONNECTION_KINDS
from hq.domains.control_plane.connection_shapes import SHAPES, projections
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.provider_spec import ConnectionShape, Setting
from hq.domains.control_plane.providers import OBSERVATION_KINDS, PROVIDERS

PACKAGE = Path(bridge_contract.__file__).parent


def _base() -> dict:
    return json.loads(bridge_contract.BASE_PATH.read_text(encoding="utf-8"))


class EmittedFilesTests(SimpleTestCase):
    def test_every_committed_file_is_the_derived_one(self):
        for path, derived in bridge_contract.emitted().items():
            with self.subTest(file=path.name):
                self.assertEqual(
                    path.read_text(encoding="utf-8"),
                    derived,
                    "run `manage.py bridge_contract`, then `go generate ./...` in controller/",
                )

    def test_the_command_reports_drift_and_writes_nothing_when_checking(self):
        derived = bridge_contract.emitted()
        stale = {path: text + " " for path, text in derived.items()}
        with patch.object(bridge_contract, "emitted", return_value=stale):
            with self.assertRaises(CommandError) as raised:
                call_command("bridge_contract", "--check", stdout=StringIO(), stderr=StringIO())
        self.assertIn("behind the registry", str(raised.exception))
        for path, text in derived.items():
            self.assertEqual(path.read_text(encoding="utf-8"), text)
        out = StringIO()
        call_command("bridge_contract", "--check", stdout=out)
        self.assertIn("current", out.getvalue())


class JoinTests(SimpleTestCase):
    def test_the_base_restates_nothing_the_registry_declares(self):
        declared = bridge_registry.schemas()
        self.assertEqual(set(_base()["components"]["schemas"]) & set(declared), set())

    def test_a_name_stated_twice_is_refused(self):
        base = _base()
        name = next(iter(base["components"]["schemas"]))
        with self.assertRaisesMessage(ValueError, "restates"):
            bridge_contract.joined(base, {name: {"type": "string"}})

    def test_a_reference_to_nothing_is_refused(self):
        with self.assertRaisesMessage(ValueError, "nothing declares"):
            bridge_contract.joined(_base(), {})

    def test_two_declarations_under_one_name_are_refused(self):
        twice = [{"Example": {"type": "string"}}, {"Example": {"type": "integer"}}]
        with patch.object(bridge_registry, "_declared", return_value=twice):
            with self.assertRaisesMessage(ValueError, "Two declarations"):
                bridge_registry.schemas()

    def test_python_reads_no_fact_back_from_a_file_it_writes(self):
        """Only ``bridge_contract`` names the written files, and only to write them."""

        written = (
            "hq-controller.openapi.json",
            "hq-connections.openapi.json",
            "controller-connections.json",
        )
        naming = []
        for path in sorted(PACKAGE.parents[1].rglob("*.py")):
            if "tests" in path.parts or path.name.startswith("test"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstrings = {
                ast.get_docstring(node, clean=False)
                for node in ast.walk(tree)
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
            }
            if any(
                name in node.value
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value not in docstrings
                for name in written
            ):
                naming.append(path.name)
        self.assertEqual(naming, ["bridge_contract.py"])

        tree = ast.parse((PACKAGE / "bridge_contract.py").read_text(encoding="utf-8"))
        paths = {"CONTRACT_PATH", "CONNECTIONS_PATH", "SHAPES_PATH"}
        emitted = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "emitted"
        )
        inside = [node.id for node in ast.walk(emitted) if isinstance(node, ast.Name)]
        read = [
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in paths
        ]
        self.assertEqual(sorted(read), sorted(paths))
        self.assertLessEqual(paths, set(inside))


class RegistrySchemaTests(SimpleTestCase):
    def setUp(self):
        self.schemas = bridge_registry.schemas()

    def test_the_kinds_are_the_registrys(self):
        self.assertEqual(
            self.schemas["ResourceKind"]["enum"], sorted(set(PROVIDERS) | set(OBSERVATION_KINDS))
        )
        self.assertEqual(self.schemas["ConnectionProvider"]["enum"], sorted(CONNECTION_KINDS))

    def test_a_swept_kind_is_a_controller_reading_or_a_resource_with_no_excuse(self):
        swept = set(self.schemas["SweptKind"]["enum"])
        for kind, spec in OBSERVATIONS.items():
            self.assertEqual(kind in swept, spec.read_by == "controller", kind)
        for kind, provider in PROVIDERS.items():
            self.assertEqual(kind in swept, not provider.unobserved_reason, kind)

    def test_every_controller_reading_has_its_record(self):
        for kind, spec in OBSERVATIONS.items():
            name = bridge_registry.record_name(kind)
            with self.subTest(kind=kind):
                if spec.read_by != "controller":
                    self.assertNotIn(name, self.schemas)
                    continue
                self.assertEqual(
                    set(self.schemas[name]["properties"]), set(spec.record.model_fields)
                )

    def test_every_enumeration_names_a_constant_for_each_value(self):
        for name, schema in self.schemas.items():
            if "x-enum-varnames" not in schema:
                continue
            with self.subTest(schema=name):
                names = schema["x-enum-varnames"]
                self.assertEqual(len(names), len(schema["enum"]))
                self.assertEqual(len(names), len(set(names)))
                for constant in names:
                    self.assertRegex(constant, r"^[A-Z][A-Za-z0-9]+$")

    def test_a_constant_is_a_function_of_its_name(self):
        for value, constant in (
            ("adguard.query_summary", "AdGuardQuerySummary"),
            ("cloudflare.d1_database", "CloudflareD1Database"),
            ("hq.request_path", "HQRequestPath"),
            ("npm.proxy_host", "NPMProxyHost"),
            ("tls.uploaded_certificate", "TLSUploadedCertificate"),
            ("github_app", "GitHubApp"),
            ("acl_management", "ACLManagement"),
            ("querylog", "QueryLog"),
            ("cpanel", "CPanel"),
            ("machine", "Machine"),
        ):
            self.assertEqual(bridge_registry.go_name(value), constant)

    def test_a_nullable_value_states_one_type(self):
        found = bridge_registry._clean(
            {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}], "title": "N"}, {}
        )
        self.assertEqual(found, {"type": ["integer", "null"], "minimum": 0})

    def test_a_property_called_title_is_kept(self):
        found = bridge_registry._clean({"properties": {"title": {"type": "string", "title": "T"}}}, {})
        self.assertEqual(found, {"properties": {"title": {"type": "string"}}})


class ConnectionShapeTests(SimpleTestCase):
    def test_every_provider_arrives_in_a_declared_shape(self):
        for provider, kind in CONNECTION_KINDS.items():
            with self.subTest(provider=provider):
                self.assertIs(SHAPES[kind.shape.name], kind.shape)

    def test_the_document_has_one_member_per_shape(self):
        schemas = bridge_registry.connections_document()["components"]["schemas"]
        connection = schemas["Connection"]["properties"]
        self.assertEqual(set(connection) - {"ref", "provider", "manages", "store"}, set(SHAPES))
        for name, shape in SHAPES.items():
            schema = schemas[bridge_registry.go_name(name)]
            with self.subTest(shape=name):
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(
                    set(schema["properties"]), {setting.name.lower() for setting in shape.settings}
                )
                self.assertEqual(
                    set(schema["required"]),
                    {setting.name.lower() for setting in shape.settings if not setting.optional},
                )
                for setting in shape.settings:
                    tags = schema["properties"][setting.name.lower()]["x-oapi-codegen-extra-tags"]
                    self.assertEqual(tags.get("secret") == "true", setting.secret)

    def test_a_credential_is_secret(self):
        for shape in SHAPES.values():
            for setting in shape.settings:
                if setting.item_field in {"credential", "password"}:
                    self.assertTrue(setting.secret, f"{shape.name}.{setting.name}")

    def test_the_renderers_registry_states_where_each_setting_comes_from(self):
        login = projections()["login"]
        self.assertEqual(login["URL"], {"source": "url", "index": 0})
        self.assertEqual(login["PASSWORD"], {"source": "field", "id": "password"})
        self.assertEqual(login["CONNECTION_REF"], {"source": "connection_ref"})
        self.assertEqual(
            projections()["github_app"]["PROVIDER"], {"source": "constant", "value": "github_app"}
        )
        self.assertEqual(
            projections()["ssh_transport"]["ROLE"],
            {"source": "field", "label": "role", "optional": True},
        )

    def test_a_shape_is_declared_whole(self):
        with self.assertRaises(ValueError):
            Setting("lower", label="x")
        with self.assertRaises(ValueError):
            Setting("TWO", label="x", url=0)
        with self.assertRaises(ValueError):
            Setting("NONE")
        with self.assertRaises(ValueError):
            ConnectionShape("Bad Name", (Setting("A", label="a"),))
        with self.assertRaises(ValueError):
            ConnectionShape("twice", (Setting("A", label="a"), Setting("A", label="b")))
        with self.assertRaises(ValueError):
            ConnectionShape("envelope", (Setting("PROVIDER", label="provider"),))
