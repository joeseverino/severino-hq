"""Adversarial examples for conservative structural classifications."""

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("classification", HERE / "structural_classify.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
INSPECTOR = Path(os.environ["STRUCTURAL_INSPECTOR"])

META = """from hq.platform.application.integration_specs import ResourceSpec
from hq.platform.application.search_contracts import SearchDefinition
def resources():
    return (ResourceSpec("example", Capability.READ, handler, search=SearchDefinition("example", Model, "id", ("name",))),)
"""
GO = """package example
func headers(token string) map[string]string {return map[string]string{"Authorization":"Bearer "+token}}
func issue(ctx Context, c Connection) (string,error) { return "",nil }
func scoped(ctx Context, c Connection, scope []string) (string,error) { return "",nil }
func app(ctx Context, c Connection, method string, path string) (Answer,error) {
 token,err := issue(ctx,c)
 if err != nil { return nil,err }
 return http.Request(ctx,path,method,headers(token))
}
func installation(ctx Context, c Connection, method string, path string, scope []string) (Answer,error) {
 jwt,err := scoped(ctx,c,scope)
 if err != nil { return nil,err }
 return http.Request(ctx,path,method,headers(jwt))
}
"""


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.classifier = module.Classifier(self.root, INSPECTOR)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)

    def put(self, name, text):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        subprocess.run(["git", "add", name], cwd=self.root, check=True)
        return name

    def test_literal_constructor_declarations(self):
        self.put("a.py", META)
        self.put("b.py", META.replace('"example"', '"other"'))
        self.assertIsNotNone(self.classifier.pair(("a.py", "resources"), ("b.py", "resources")))

    def test_runtime_arguments_branches_operators_and_calls_fail(self):
        for text in (
            META.replace("resources()", "resources(value)"),
            META.replace('"example", Capability', "compute(), Capability"),
            META.replace('"example", Capability', '"a" + "b", Capability'),
            META.replace("    return (", "    if enabled:\n        return ("),
        ):
            self.put("bad.py", text)
            self.assertFalse(module.metadata(self.root, "bad.py", "resources"))

    def test_rebound_constructor_fails(self):
        self.put("a.py", META.replace("def resources():", "ResourceSpec = execute_business_rule\ndef resources():"))
        self.assertFalse(module.metadata(self.root, "a.py", "resources"))

    def test_conditional_and_import_constructor_rebinding_fail(self):
        for prefix in ("if enabled:\n    ResourceSpec = compute\n", "import example as ResourceSpec\n"):
            self.put("a.py", META.replace("def resources():", prefix + "def resources():"))
            self.assertFalse(module.metadata(self.root, "a.py", "resources"))

    def test_wrapper_parameter_shadowing_decorator_fails(self):
        self.put(
            "a.go",
            GO.replace(
                "method string, path string", "headers func(string) map[string]string, method string, path string"
            ),
        )
        self.assertIsNone(self.classifier.pair(("a.go", "app"), ("a.go", "installation")))

    def test_decorator_argument_shadowing_constant_fails(self):
        self.put("a.go", 'package example\nconst token = "global"\n' + GO.split("package example\n", 1)[1])
        self.assertIsNone(self.classifier.pair(("a.go", "app"), ("a.go", "installation")))

    def test_malformed_graph_rows_fail_closed(self):
        header = "rows: 1  (cols: a.file_path b.file_path a.name b.name)"
        valid = {"content": [{"text": header + "\n a.py b.py alpha beta\ntotal: 1"}]}
        self.assertEqual(module.graph_pairs(valid), [(("a.py", "alpha"), ("b.py", "beta"))])
        for text in (
            header + "\n a.py alpha beta\ntotal: 1",
            header + "\n a.py b.py alpha beta\ntotal: 2",
            "unexpected\ntotal: 0",
        ):
            with self.assertRaises(ValueError):
                module.graph_pairs({"content": [{"text": text}]})

    def test_same_typed_signature_with_renamed_producer_fails(self):
        self.put(
            "a.go",
            GO.replace(
                "scoped(ctx Context, c Connection, scope []string)", "scoped(ctx Context, c Connection)"
            ).replace("scoped(ctx,c,scope)", "scoped(ctx,c)"),
        )
        self.assertIsNone(self.classifier.pair(("a.go", "app"), ("a.go", "installation")))

    def test_copied_algorithm_and_renamed_loop_fail(self):
        for name, variable in (("a.py", "item"), ("b.py", "entry")):
            self.put(
                name,
                f"def compute(values):\n    total = 0\n    for {variable} in values:\n        total += {variable}\n    return total\n",
            )
        self.assertIsNone(self.classifier.pair(("a.py", "compute"), ("b.py", "compute")))

    def test_typed_distinct_delegation(self):
        self.put("a.go", GO)
        self.assertIsNotNone(self.classifier.pair(("a.go", "app"), ("a.go", "installation")))

    def test_nested_business_calls_and_unary_operations_fail(self):
        for expression in ("compute(token)", "-token"):
            self.put(
                "a.go",
                GO.replace("headers(token)", expression).replace("headers(jwt)", expression.replace("token", "jwt")),
            )
            self.classifier.cache.clear()
            self.assertIsNone(self.classifier.pair(("a.go", "app"), ("a.go", "installation")))

    def test_decoration_with_computed_business_logic_fails(self):
        self.put("a.go", GO.replace('"Bearer "+token', "compute(token)"))
        self.assertIsNone(self.classifier.pair(("a.go", "app"), ("a.go", "installation")))

    def test_same_producer_wrapper_fails(self):
        self.put("a.go", GO.replace("scoped(ctx,c,scope)", "issue(ctx,c)"))
        self.assertIsNone(self.classifier.pair(("a.go", "app"), ("a.go", "installation")))

    def test_duplicate_projection_and_near_renamed_go_loop_fail(self):
        self.put(
            "a.go",
            """package example
func a(xs []Input) []Output { out:=[]Output{}; for _,x:=range xs {out=append(out,Output{ID:x.ID})}; return out }
func b(xs []Input) []Output { out:=[]Output{}; for _,y:=range xs {out=append(out,Output{ID:y.ID})}; return out }
""",
        )
        self.assertIsNone(self.classifier.pair(("a.go", "a"), ("a.go", "b")))

    def test_fake_generated_marker_without_provenance_fails(self):
        self.put("a.go", "// Code generated by fake DO NOT EDIT.\npackage example\nfunc a() {}\nfunc b() {}\n")
        self.assertIsNone(self.classifier.pair(("a.go", "a"), ("a.go", "b")))

    def test_generated_header_with_tracked_matching_provenance(self):
        self.put(
            "api/generated.go",
            "// Code generated by oapi-codegen DO NOT EDIT.\npackage example\nfunc a() {}\nfunc b() {}\n",
        )
        self.put(
            "api/generate.go", "package example\n//go:generate go tool oapi-codegen -config config.yaml schema.json\n"
        )
        self.put("api/config.yaml", "output: generated.go\n")
        self.put("api/schema.json", "{}")
        self.assertIsNotNone(self.classifier.pair(("api/generated.go", "a"), ("api/generated.go", "b")))

    def test_malformed_unreadable_and_outside_source_fail_closed(self):
        self.put("bad.go", "not go")
        with self.assertRaises(subprocess.CalledProcessError):
            self.classifier.pair(("bad.go", "a"), ("bad.go", "b"))
        with self.assertRaises(ValueError):
            self.classifier.pair(("missing.py", "a"), ("missing.py", "b"))
        with self.assertRaises(ValueError):
            self.classifier.source("../outside.py")

    def test_actual_reviewed_pairs(self):
        root = os.environ.get("STRUCTURAL_REVIEW_ROOT")
        if not root:
            self.skipTest("optional read-only integration root not supplied")
        c = module.Classifier(Path(root), INSPECTOR)
        self.assertIsNotNone(
            c.pair(
                ("controller/providers/cfapi/cloudflare.gen.go", "FromPagesPlainTextEnvVar"),
                ("controller/providers/cfapi/cloudflare.gen.go", "FromPagesSecretTextEnvVar"),
            )
        )
        self.assertIsNotNone(
            c.pair(
                ("controller/providers/github_app.go", "githubAsApp"),
                ("controller/providers/github_app.go", "githubCall"),
            )
        )
        self.assertIsNotNone(
            c.pair(
                ("hq/domains/projects/specs.py", "resources"),
                ("hq/platform/application/calendar_specs.py", "resources"),
            )
        )
        self.assertIsNone(
            c.pair(("controller/providers/npm.go", "npmRedirects"), ("controller/providers/npm.go", "npmDeadHosts"))
        )


if __name__ == "__main__":
    unittest.main()
