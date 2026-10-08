"""Regression checks for vendor slice narrowing and upstream contracts."""

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("vendor_slice", Path(__file__).with_name("slice.py"))
slicer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(slicer)


class SliceTests(unittest.TestCase):
    def test_property_narrowing_preserves_type_and_required(self):
        schema = {
            "properties": {"read": {"type": "integer"}, "unused": {"type": "string"}},
            "required": ["read", "unused"],
        }
        slicer.narrow("model", schema, ["read"])
        self.assertEqual(schema, {"properties": {"read": {"type": "integer"}}, "required": ["read"]})

    def test_union_narrowing_keeps_shared_allof_parts(self):
        schema = {
            "oneOf": [
                {"allOf": [{"$ref": slicer.SCHEMAS + name}, {"$ref": slicer.SCHEMAS + "shared"}]}
                for name in ("read", "unused")
            ]
        }
        slicer.narrow("model", schema, ["read"])
        self.assertEqual(len(schema["oneOf"]), 1)
        self.assertEqual(slicer.built_on(schema["oneOf"][0]), {"read", "shared"})

    def test_unknown_property_and_variant_fail(self):
        for schema in ({"properties": {"read": {"type": "string"}}}, {"anyOf": [{"$ref": slicer.SCHEMAS + "read"}]}):
            with self.subTest(schema=schema), self.assertRaisesRegex(SystemExit, "does not have"):
                slicer.narrow("model", schema, ["stale"])

    def run_slice(self, settings, servers=({"url": "https://api.example.com"},)):
        upstream = {
            "openapi": "3.0.0",
            "info": {"title": "Example", "version": "1"},
            "servers": list(servers),
            "paths": {
                "/example/{id}": {
                    "get": {
                        "operationId": "read",
                        "parameters": [
                            {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}},
                            {"name": "unused", "in": "query", "schema": {"type": "string"}},
                        ],
                        "responses": {
                            "200": {
                                "description": "ok",
                                "content": {"application/json": {"schema": {"$ref": slicer.SCHEMAS + "answer"}}},
                            }
                        },
                    }
                }
            },
            "components": {
                "schemas": {
                    "answer": {"type": "object", "properties": {"read": {"type": "integer"}}},
                    "unrelated": {"type": "string"},
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "upstream.json").write_text(json.dumps(upstream))
            (root / "slice.toml").write_text(settings)
            (root / "oapi-codegen.yaml").write_text("include-operation-ids:\n  - read\n")
            previous = Path.cwd()
            try:
                os.chdir(root)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    slicer.main(root / "upstream.json")
                return json.loads(output.getvalue())
            finally:
                os.chdir(previous)

    def test_operations_drop_query_and_answer_but_keep_path(self):
        result = self.run_slice('[decodes]\nread = ["answer"]\n')
        operation = result["paths"]["/example/{id}"]["get"]
        self.assertEqual(
            operation["parameters"], [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}]
        )
        self.assertEqual(operation["responses"], {"200": {"description": "ok"}})
        self.assertEqual(set(result["components"]["schemas"]), {"answer"})

    def test_the_slice_keeps_the_servers_and_refuses_a_spec_without_any(self):
        result = self.run_slice('[decodes]\nread = ["answer"]\n')
        self.assertEqual(result["servers"], [{"url": "https://api.example.com"}])
        with self.assertRaisesRegex(SystemExit, "names no servers"):
            self.run_slice("", servers=())

    def test_unreachable_decode_and_stale_configuration_fail(self):
        for settings, message in (
            ('[decodes]\nread = ["unrelated"]\n', "answers no"),
            ("[keep]\nunrelated = []\n", "slice does not hold"),
            ('[go-names]\nunrelated = "Unused"\n', "slice does not hold"),
            ('sends = ["unknown"]\n', "names operations"),
        ):
            with self.subTest(settings=settings), self.assertRaisesRegex(SystemExit, message):
                self.run_slice(settings)


if __name__ == "__main__":
    unittest.main()
