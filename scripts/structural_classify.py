"""Conservative AST classifications for graph similarity, not a policy proof."""
from __future__ import annotations

import ast
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

CONSTRUCTORS = {
    "hq.platform.application.integration_specs.ResourceSpec",
    "hq.platform.application.search_contracts.SearchDefinition",
}


def constructor_bindings(tree, path):
    bindings = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            package = list(Path(path).with_suffix("").parts[:-1])
            prefix = ".".join(package[:len(package) - node.level + 1]) if node.level else ""
            imported = ".".join(p for p in (prefix, node.module) if p)
            for alias in node.names:
                bindings[alias.asname or alias.name] = f"{imported}.{alias.name}"
    constructors_bound = {name for name, target in bindings.items() if target in CONSTRUCTORS}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id in constructors_bound:
            return {}
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in constructors_bound:
            return {}
        if isinstance(node, ast.Import) and any((alias.asname or alias.name.split(".")[0]) in constructors_bound for alias in node.names):
            return {}
    return bindings


def metadata(root: Path, path: str, name: str) -> bool:
    tree = ast.parse((root / path).read_text())
    bindings = constructor_bindings(tree, path)
    candidates = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    if len(candidates) != 1:
        return False
    function = candidates[0]
    args = function.args
    if args.posonlyargs or args.args or args.kwonlyargs or args.vararg or args.kwarg or function.decorator_list:
        return False
    if len(function.body) != 1 or not isinstance(function.body[0], ast.Return):
        return False
    constructors = []

    def literal(node):
        if isinstance(node, (ast.Constant, ast.Name)):
            return True
        if isinstance(node, ast.Attribute):
            return literal(node.value) and isinstance(node.value, (ast.Name, ast.Attribute))
        if isinstance(node, ast.Tuple):
            return all(literal(child) for child in node.elts)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and bindings.get(node.func.id) in CONSTRUCTORS:
            constructors.append(bindings[node.func.id])
            return all(literal(child) for child in node.args) and all(k.arg is not None and literal(k.value) for k in node.keywords)
        return False

    return literal(function.body[0].value) and bool(constructors)


class Classifier:
    def __init__(self, root: Path, inspector: Path):
        self.root = root.resolve()
        self.inspector = inspector
        self.cache = {}

    def source(self, path):
        resolved = (self.root / path).resolve()
        if not resolved.is_relative_to(self.root) or not resolved.is_file():
            raise ValueError(f"missing/outside source: {path}")
        return resolved

    def go(self, path):
        source = self.source(path)
        if path not in self.cache:
            self.cache[path] = json.loads(subprocess.check_output([str(self.inspector), str(source)], text=True))
        return self.cache[path]

    def tracked(self, path):
        subprocess.run(["git", "ls-files", "--error-unmatch", "--", str(path.relative_to(self.root))], cwd=self.root, check=True, capture_output=True)

    def generated(self, path):
        if not self.go(path)["Generated"]:
            return False
        source = self.source(path)
        self.tracked(source)
        return self.generator_matches(source)

    def generator_matches(self, source):
        for adjacent in source.parent.glob("*.go"):
            if adjacent == source:
                continue
            self.tracked(adjacent)
            for directive in self.go(str(adjacent.relative_to(self.root)))["Directives"] or []:
                words = shlex.split(directive)
                if len(words) != 6 or words[:4] != ["go", "tool", "oapi-codegen", "-config"]:
                    continue
                config = (adjacent.parent / words[4]).resolve()
                schema = (adjacent.parent / words[5]).resolve()
                for item in (config, schema):
                    if not item.is_relative_to(self.root) or not item.is_file():
                        raise ValueError("missing/outside generator provenance")
                    self.tracked(item)
                # Only the simple YAML scalar output supported by the actual configs;
                # ambiguous/duplicate/nested declarations fail closed.
                outputs = re.findall(r"^output: ([^\s#\"']+)$", config.read_text(), re.MULTILINE)
                if len(outputs) == 1 and (adjacent.parent / outputs[0]).resolve() == source:
                    return True
        return False

    def pair(self, a, b):
        self.source(a[0])
        self.source(b[0])
        if a[0].endswith(".go") and b[0].endswith(".go"):
            if not self.go(a[0])["Functions"].get(a[1]) or not self.go(b[0])["Functions"].get(b[1]):
                raise ValueError("graph symbol absent from parsed Go source")
            if self.generated(a[0]) and self.generated(b[0]):
                return "generated provenance (adjacent tracked directive/config/schema; regeneration gate required)"
            x = self.go(a[0])["Wrappers"].get(a[1])
            y = self.go(b[0])["Wrappers"].get(b[1])
            if x and y and x["Producer"] != y["Producer"] and x["ProducerSignature"] != y["ProducerSignature"] and x["Signature"] != y["Signature"] and x["Forward"] == y["Forward"]:
                return "typed delegation (distinct local string/error producer signatures; identical forward)"
        if a[0].endswith(".py") and b[0].endswith(".py") and metadata(self.root, *a) and metadata(self.root, *b):
            return "immutable metadata constructor declarations (no arguments/control/computed calls)"
        return None


def graph_pairs(payload):
    if payload.get("isError"):
        raise ValueError("graph query failed")
    text = payload["content"][0]["text"]
    lines = text.splitlines()
    header = re.fullmatch(r"rows: (\d+)  \(cols: a\.file_path b\.file_path a\.name b\.name\)", lines[0])
    if not header or not re.fullmatch(r"total: \d+", lines[-1]):
        raise ValueError("unexpected graph header/footer")
    count = int(header[1])
    rows = lines[1:-1]
    if len(rows) != count or int(lines[-1].split()[1]) != count:
        raise ValueError("graph row count mismatch")
    result = []
    for row in rows:
        cells = row.split()
        if len(cells) != 4:
            raise ValueError("malformed similarity row")
        result.append(((cells[0], cells[2]), (cells[1], cells[3])))
    return result


def main():
    root, inspector = Path(sys.argv[1]), Path(sys.argv[2])
    classifier = Classifier(root, inspector)
    pairs = set()
    test = re.compile(r"(^|/)(test_[^/]*|tests|[^/]*_tests)\.py$|(^|/)tests/|_test\.go$")
    for a, b in graph_pairs(json.load(sys.stdin)):
        if test.search(a[0]) or test.search(b[0]):
            continue
        reason = classifier.pair(a, b)
        pair = " <> ".join(sorted(f"{path}::{name}" for path, name in (a, b)))
        if reason:
            print(f"classified {pair}: {reason}", file=sys.stderr)
        else:
            pairs.add("similar " + pair)
    print("\n".join(sorted(pairs)))


if __name__ == "__main__":
    main()
