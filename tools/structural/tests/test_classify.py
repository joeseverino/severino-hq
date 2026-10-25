"""Adversarial examples for conservative structural classifications."""

import json
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, tag

from tools.structural import classify as module
from tools.structural.bar import build_inspector

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


FP = "0" * 512


def unit(path, name, owner="", fp=FP):
    return module.Unit(path, name, owner, tuple(fp[i : i + 8] for i in range(0, 512, 8)))


def slots(agreeing):
    """A fingerprint agreeing with FP on this many of its 64 slots."""

    return "0" * 8 * agreeing + "f" * 8 * (64 - agreeing)


def page(rows, total=None, more=False, relation="eq", cols=module.COLUMNS):
    answer = {
        "columns": cols,
        "rows": rows,
        "total": len(rows) if total is None else total,
        "total_relation": relation,
        "has_more": more,
    }
    return {"content": [{"text": json.dumps(answer)}]}


class FingerprintTests(SimpleTestCase):
    def test_a_pair_is_near_from_the_threshold_up_and_only_within_one_language(self):
        near = [unit("a.py", "a"), unit("b.py", "b", fp=slots(55)), unit("c.go", "c")]
        far = [unit("a.py", "a"), unit("b.py", "b", fp=slots(54))]
        self.assertEqual([(a.name, b.name) for a, b in module.near_pairs(near)], [("a", "b")])
        self.assertEqual(list(module.near_pairs(far)), [])

    def test_banding_finds_a_pair_whose_differing_slots_spoil_the_most_bands(self):
        """Nine differing slots, one in each of nine bands: 23 bands still match whole."""

        spread = "".join("f" * 8 if i % 2 == 0 and i < 18 else "0" * 8 for i in range(64))
        self.assertEqual(len(list(module.near_pairs([unit("a.py", "a"), unit("b.py", "b", fp=spread)]))), 1)

    def test_a_test_by_its_path_or_its_name_is_left_out(self):
        for path, name in (("pkg/tests/test_a.py", "helper"), ("demo/check.py", "test_pages"), ("x_test.go", "f")):
            self.assertTrue(module.is_test(unit(path, name)))
        self.assertFalse(module.is_test(unit("pkg/a.py", "tested")))

    def test_a_body_too_small_to_compare_is_skipped_and_the_owner_is_its_class(self):
        units = module.fingerprints([page([["a.py", "small", "", ""], ["a.py", "big", "probe.a.Owner", FP[:-1]]])])
        self.assertEqual([(u.name, u.owner, u.label()) for u in units], [("big", "Owner", "a.py::Owner.big")])

    def test_malformed_partial_or_empty_answers_fail_closed(self):
        row = ["a.py", "alpha", "", FP]
        self.assertEqual(len(module.fingerprints([page([row], 2, more=True), page([row], 2)])), 2)
        for pages in (
            [page([row[:3]])],
            [page([["a.py", "alpha", "", "xyz"]])],
            [page([row], 2)],
            [page([row], 1, more=True)],
            [page([row], relation="gte")],
            [page([row], cols=module.COLUMNS[:3])],
            [page([row], 2, more=True), page([row], 3)],
            [page([["a.py", "alpha", "", ""]])],
            [],
        ):
            with self.assertRaises(ValueError):
                module.fingerprints(pages)


@tag("source")
class ClassificationTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        built = tempfile.TemporaryDirectory()
        cls.addClassCleanup(built.cleanup)
        cls.inspector = build_inspector(Path(built.name))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.classifier = module.Classifier(self.root, self.inspector)
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
        self.assertIsNotNone(self.classifier.pair(unit("a.py", "resources"), unit("b.py", "resources")))

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
        self.assertIsNone(self.classifier.pair(unit("a.go", "app"), unit("a.go", "installation")))

    def test_decorator_argument_shadowing_constant_fails(self):
        self.put("a.go", 'package example\nconst token = "global"\n' + GO.split("package example\n", 1)[1])
        self.assertIsNone(self.classifier.pair(unit("a.go", "app"), unit("a.go", "installation")))

    def test_same_typed_signature_with_renamed_producer_fails(self):
        self.put(
            "a.go",
            GO.replace(
                "scoped(ctx Context, c Connection, scope []string)", "scoped(ctx Context, c Connection)"
            ).replace("scoped(ctx,c,scope)", "scoped(ctx,c)"),
        )
        self.assertIsNone(self.classifier.pair(unit("a.go", "app"), unit("a.go", "installation")))

    def test_copied_algorithm_and_renamed_loop_fail(self):
        for name, variable in (("a.py", "item"), ("b.py", "entry")):
            self.put(
                name,
                f"def compute(values):\n    total = 0\n    for {variable} in values:\n        total += {variable}\n    return total\n",
            )
        self.assertIsNone(self.classifier.pair(unit("a.py", "compute"), unit("b.py", "compute")))

    def test_typed_distinct_delegation(self):
        self.put("a.go", GO)
        self.assertIsNotNone(self.classifier.pair(unit("a.go", "app"), unit("a.go", "installation")))

    def test_nested_business_calls_and_unary_operations_fail(self):
        for expression in ("compute(token)", "-token"):
            self.put(
                "a.go",
                GO.replace("headers(token)", expression).replace("headers(jwt)", expression.replace("token", "jwt")),
            )
            self.classifier.cache.clear()
            self.assertIsNone(self.classifier.pair(unit("a.go", "app"), unit("a.go", "installation")))

    def test_decoration_with_computed_business_logic_fails(self):
        self.put("a.go", GO.replace('"Bearer "+token', "compute(token)"))
        self.assertIsNone(self.classifier.pair(unit("a.go", "app"), unit("a.go", "installation")))

    def test_same_producer_wrapper_fails(self):
        self.put("a.go", GO.replace("scoped(ctx,c,scope)", "issue(ctx,c)"))
        self.assertIsNone(self.classifier.pair(unit("a.go", "app"), unit("a.go", "installation")))

    def test_duplicate_projection_and_near_renamed_go_loop_fail(self):
        self.put(
            "a.go",
            """package example
func a(xs []Input) []Output { out:=[]Output{}; for _,x:=range xs {out=append(out,Output{ID:x.ID})}; return out }
func b(xs []Input) []Output { out:=[]Output{}; for _,y:=range xs {out=append(out,Output{ID:y.ID})}; return out }
""",
        )
        self.assertIsNone(self.classifier.pair(unit("a.go", "a"), unit("a.go", "b")))

    def test_fake_generated_marker_without_provenance_fails(self):
        self.put("a.go", "// Code generated by fake DO NOT EDIT.\npackage example\nfunc a() {}\nfunc b() {}\n")
        self.assertIsNone(self.classifier.pair(unit("a.go", "a"), unit("a.go", "b")))

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
        self.assertIsNotNone(self.classifier.pair(unit("api/generated.go", "a"), unit("api/generated.go", "b")))

    def test_malformed_unreadable_and_outside_source_fail_closed(self):
        self.put("bad.go", "not go")
        with self.assertRaises(subprocess.CalledProcessError):
            self.classifier.pair(unit("bad.go", "a"), unit("bad.go", "b"))
        with self.assertRaises(ValueError):
            self.classifier.pair(unit("missing.py", "a"), unit("missing.py", "b"))
        with self.assertRaises(ValueError):
            self.classifier.source("../outside.py")

    def test_dict_projections_over_disjoint_keys(self):
        self.put(
            "a.py", 'def path(p):\n    """A path."""\n    return {"host": p.host, "hops": [h.name for h in p.hops]}\n'
        )
        self.put(
            "b.py",
            'class View:\n    def sight(self, s):\n        return {"provider": s.provider, "tally": list(s.tally)}\n',
        )
        self.assertIsNotNone(self.classifier.pair(unit("a.py", "path"), unit("b.py", "sight", "View")))

    def test_projections_sharing_a_key_or_doing_more_fail(self):
        self.put("a.py", 'def path(p):\n    return {"host": p.host, "hops": p.hops}\n')
        for text in (
            'def other(p):\n    return {"host": p.name, "line": p.line}\n',
            'def other(p):\n    p.touch()\n    return {"line": p.line}\n',
            'def other(p):\n    return {"line": p.line, **p.extra}\n',
            "def other(p):\n    return {}\n",
        ):
            self.put("b.py", text)
            self.assertIsNone(self.classifier.pair(unit("a.py", "path"), unit("b.py", "other")))

    def test_bodies_that_are_one_call_of_the_same_function(self):
        self.put(
            "a.go",
            """package example
func shared[T any](ref string, build func() T) T { return build() }
func other(ref string, build func() int) int { return build() }
func redirects(ref string) int { return shared(ref, func() int { return 1 }) }
func dead(ref string) string { return shared(ref, func() string { return "" }) }
func elsewhere(ref string) int { return other(ref, func() int { return 1 }) }
""",
        )
        self.assertIsNotNone(self.classifier.pair(unit("a.go", "redirects"), unit("a.go", "dead")))
        self.assertIsNone(self.classifier.pair(unit("a.go", "redirects"), unit("a.go", "elsewhere")))

    def test_switch_tables_over_disjoint_cases_and_a_shared_case_fails(self):
        table = """func %s(err error) *E {
	var e %s
	reason := ""
	switch {
	case errors.As(err, &e):
		reason = "%s"
	case errors.Is(err, %s):
		reason = "refused"
	default:
		reason = fallback(err, "failed")
	}
	return &E{reason: reason}
}
"""
        self.put(
            "a.go",
            "package example\n"
            + table % ("dial", "*net.DNSError", "no name", "io.EOF")
            + table % ("handshake", "*tls.Error", "untrusted", "io.ErrUnexpectedEOF")
            + table % ("renamed", "*net.DNSError", "other words", "io.ErrClosedPipe"),
        )
        self.assertIsNotNone(self.classifier.pair(unit("a.go", "dial"), unit("a.go", "handshake")))
        self.assertIsNone(self.classifier.pair(unit("a.go", "dial"), unit("a.go", "renamed")))
