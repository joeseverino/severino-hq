"""Conservative AST classifications for graph similarity, not a policy proof."""

from __future__ import annotations

import ast
import json
import re
import shlex
import subprocess
import sys
from collections import defaultdict
from functools import cache, cached_property
from itertools import combinations
from pathlib import Path
from typing import NamedTuple

CONSTRUCTORS = {
    "hq.platform.application.integration_specs.ResourceSpec",
    "hq.platform.application.search_contracts.SearchDefinition",
}


def constructor_bindings(tree, path):
    bindings = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            package = list(Path(path).with_suffix("").parts[:-1])
            prefix = ".".join(package[: len(package) - node.level + 1]) if node.level else ""
            imported = ".".join(p for p in (prefix, node.module) if p)
            for alias in node.names:
                bindings[alias.asname or alias.name] = f"{imported}.{alias.name}"
    constructors_bound = {name for name, target in bindings.items() if target in CONSTRUCTORS}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id in constructors_bound:
            return {}
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in constructors_bound:
            return {}
        if isinstance(node, ast.Import) and any(
            (alias.asname or alias.name.split(".")[0]) in constructors_bound for alias in node.names
        ):
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
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and bindings.get(node.func.id) in CONSTRUCTORS
        ):
            constructors.append(bindings[node.func.id])
            return all(literal(child) for child in node.args) and all(
                k.arg is not None and literal(k.value) for k in node.keywords
            )
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

    @cached_property
    def tracked_files(self):
        # One git ls-files for the run, not one git process per path.
        listed = subprocess.check_output(["git", "ls-files", "-z"], cwd=self.root)
        return frozenset(name.decode() for name in listed.split(b"\0") if name)

    def tracked(self, path):
        if str(path.relative_to(self.root)) not in self.tracked_files:
            raise ValueError(f"not tracked: {path}")

    @cache  # noqa: B019 - one classifier per run, and a path's provenance does not change within it
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
        """Why a near pair of units is no duplicate, or None when it is one."""

        self.source(a.path)
        self.source(b.path)
        if a.path.endswith(".go") and b.path.endswith(".go"):
            return self.go_pair(a.path, a.name, b.path, b.name)
        if a.path.endswith(".py") and b.path.endswith(".py"):
            if metadata(self.root, a.path, a.name) and metadata(self.root, b.path, b.name):
                return "immutable metadata constructor declarations (no arguments/control/computed calls)"
            x = projection(self.root, a)
            y = projection(self.root, b)
            if x and y and not x & y:
                return "dict projections over disjoint keys"
        return None

    def go_pair(self, a, a_name, b, b_name):
        one, other = self.go(a), self.go(b)
        if not one["Functions"].get(a_name) or not other["Functions"].get(b_name):
            raise ValueError("graph symbol absent from parsed Go source")
        if self.generated(a) and self.generated(b):
            return "generated provenance (adjacent tracked directive/config/schema; regeneration gate required)"
        x = one["Wrappers"].get(a_name)
        y = other["Wrappers"].get(b_name)
        if (
            x
            and y
            and x["Producer"] != y["Producer"]
            and x["ProducerSignature"] != y["ProducerSignature"]
            and x["Signature"] != y["Signature"]
            and x["Forward"] == y["Forward"]
        ):
            return "typed delegation (distinct local string/error producer signatures; identical forward)"
        # Both sides already share their logic: each body is one call of the same function.
        if (
            a == b
            and one["Delegations"].get(a_name)
            and one["Delegations"].get(a_name) == other["Delegations"].get(b_name)
        ):
            return "delegation to one shared same-file function"
        x = one["Tables"].get(a_name)
        y = other["Tables"].get(b_name)
        if x and y and x["Return"] == y["Return"] and x["Cases"] and not set(x["Cases"]) & set(y["Cases"]):
            return "switch tables over disjoint cases"
        return None


def projection(root: Path, unit) -> frozenset[str] | None:
    """The keys of a function that only returns a dict literal with string
    keys, or None. Two such projections over different keys share only the
    literal's shape."""

    tree = ast.parse((root / unit.path).read_text())
    scopes = (
        [tree] if not unit.owner else [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == unit.owner]
    )
    candidates = [
        n
        for scope in scopes
        for n in scope.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == unit.name
    ]
    if len(candidates) != 1:
        return None
    body = candidates[0].body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    if len(body) != 1 or not isinstance(body[0], ast.Return) or not isinstance(body[0].value, ast.Dict):
        return None
    keys = body[0].value.keys
    if not keys or not all(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in keys):
        return None
    return frozenset(k.value for k in keys)


COLUMNS = ["f.file_path", "f.name", "f.parent_class", "f.fp"]
SLOTS = 64
# A pair is near when this share of its MinHash slots agree. The graph draws
# SIMILAR_TO only at 0.95, where every pair outside tests is generated code;
# 0.85 is where the pairs a reader finds by hand appear and the shapes that
# are not duplicates can still be named.
THRESHOLD = 0.85
BANDS = 32


class Unit(NamedTuple):
    path: str
    name: str
    owner: str
    slots: tuple[str, ...]

    def label(self) -> str:
        return f"{self.path}::{self.owner + '.' if self.owner else ''}{self.name}"


def fingerprints(pages):
    """The fingerprinted functions of one query, from every page of its answer,
    refused unless the pages are whole: a partial answer would read as fewer
    duplicates than there are."""

    rows, total = [], None
    for payload in pages:
        if payload.get("isError"):
            raise ValueError("graph query failed")
        answer = json.loads(payload["content"][0]["text"])
        if answer.get("columns") != COLUMNS:
            raise ValueError("unexpected graph columns")
        if answer.get("total_relation") != "eq" or total not in (None, answer.get("total")):
            raise ValueError("graph total is not exact")
        total = answer["total"]
        rows += answer["rows"]
    if total is None or not pages or (pages[-1] and json.loads(pages[-1]["content"][0]["text"]).get("has_more")):
        raise ValueError("graph answer is not whole")
    if len(rows) != total:
        raise ValueError("graph row count mismatch")
    units = []
    for row in rows:
        if len(row) != 4 or not all(isinstance(cell, str) for cell in row) or not row[0] or not row[1]:
            raise ValueError("malformed function row")
        path, name, owner, fp = row
        if not fp:
            # The graph leaves a body too small to compare without one.
            continue
        # 64 slots of 8 hex digits; the query answers all but the last digit,
        # so the last slot compares on its first seven, as the graph's own
        # SIMILAR_TO scores agree.
        if not re.fullmatch(f"[0-9a-f]{{{SLOTS * 8 - 1},{SLOTS * 8}}}", fp):
            raise ValueError("malformed fingerprint")
        units.append(Unit(path, name, owner.rsplit(".", 1)[-1], tuple(fp[i : i + 8] for i in range(0, SLOTS * 8, 8))))
    if not units:
        raise ValueError("graph holds no fingerprinted function: the index is empty")
    return units


def near_pairs(units):
    """Each pair of same-language units whose fingerprints agree on THRESHOLD
    of their slots. Agreeing on 55 of 64 slots leaves at most 9 that differ, so
    at least 23 of the 32 two-slot bands match whole: banding finds every such
    pair, never a sample of them."""

    buckets = defaultdict(list)
    for index, unit in enumerate(units):
        for band in range(BANDS):
            buckets[(band, unit.slots[2 * band], unit.slots[2 * band + 1])].append(index)
    seen = set()
    for members in buckets.values():
        for i, j in combinations(members, 2):
            if (i, j) in seen:
                continue
            seen.add((i, j))
            a, b = units[i], units[j]
            if Path(a.path).suffix != Path(b.path).suffix:
                continue
            if sum(x == y for x, y in zip(a.slots, b.slots, strict=True)) >= THRESHOLD * SLOTS:
                yield a, b


TEST = re.compile(r"(^|/)(test_[^/]*|tests|[^/]*_tests)\.py$|(^|/)tests/|_test\.go$")


def is_test(unit):
    return bool(TEST.search(unit.path)) or (unit.path.endswith(".py") and unit.name.startswith("test_"))


def similar_pairs(root: Path, inspector: Path, pages: list[dict]) -> list[str]:
    """Every near-duplicate pair outside tests that no classification explains, as baseline lines."""

    classifier = Classifier(root, inspector)
    pairs = set()
    explained = 0
    for a, b in near_pairs([unit for unit in fingerprints(pages) if not is_test(unit)]):
        if classifier.pair(a, b):
            explained += 1
        else:
            pairs.add("similar " + " <> ".join(sorted((a.label(), b.label()))))
    # One line, so a gate's verdict is never pushed out of view by pairs that pass.
    print(f"{explained} similar pairs explained by a classification", file=sys.stderr)
    return sorted(pairs)
