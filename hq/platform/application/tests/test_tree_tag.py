"""A source test that reads a whole tree carries the ``tree`` tag, and only one.

No import leads from a changed module to a test that finds it by walking a
directory, so ``mise run fast`` runs every ``tree`` test on any change to a
module under hq/ or hq_sdk/. A walk is ``rglob``, ``os.walk`` or ``git
ls-files``, in the test, its class's setup, or any function or value of this
checkout it reaches by name.
"""

import ast
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import dedent

from django.test import SimpleTestCase, tag

from .source import parsed, source_files

ROOT = Path(__file__).resolve().parents[4]
SUITE = ("hq", "hq_sdk", "tools", "tests")
SETUP = ("setUp", "setUpClass", "setUpTestData")

# A definition found by name: the module it lives in and its node.
Found = tuple[str, ast.AST]


def is_walk(node: ast.AST) -> bool:
    """``x.rglob(...)``, ``os.walk(...)``, or ``ls-files`` in a string."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return "ls-files" in node.value.split()
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    owner = node.func.value
    return node.func.attr == "rglob" or (node.func.attr == "walk" and isinstance(owner, ast.Name) and owner.id == "os")


def tags(decorators: list[ast.expr]) -> set[str]:
    found: set[str] = set()
    for decorator in decorators:
        if (
            isinstance(decorator, ast.Call)
            and getattr(decorator.func, "id", getattr(decorator.func, "attr", "")) == "tag"
        ):
            found.update(arg.value for arg in decorator.args if isinstance(arg, ast.Constant))
    return found


class Module:
    """One module's top-level names: what each defines, and what each imports."""

    def __init__(self, dotted: str, path: Path):
        package = dotted if path.name == "__init__.py" else dotted.rpartition(".")[0]
        self.defined: dict[str, ast.AST] = {}
        # A local name -> (the module it came from, the name there, or None for the module itself).
        self.imported: dict[str, tuple[str, str | None]] = {}
        for node in parsed(path).body:
            self.read(node, package)

    def read(self, node: ast.stmt, package: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            self.defined[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            self.defined.update((target.id, node.value) for target in targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.partition(".")[0]
                self.imported[local] = (alias.name if alias.asname else local, None)
        elif isinstance(node, ast.ImportFrom):
            head = node.module or ""
            if node.level:
                base = package.split(".")[: len(package.split(".")) - node.level + 1]
                head = ".".join([*base, *([node.module] if node.module else [])])
            for alias in node.names:
                self.imported[alias.asname or alias.name] = (head, alias.name)


@dataclass(frozen=True)
class Unit:
    """One test method: its label, its tags and where its own tags stand."""

    label: str
    tags: frozenset[str]
    on_method: bool
    walks: bool


class Walks:
    """Which definitions of a checkout reach a walk, followed by name across its modules."""

    def __init__(self, root: Path):
        self.root = root
        self.modules: dict[str, Module | None] = {}
        self.known: dict[int, bool] = {}

    def module(self, dotted: str) -> Module | None:
        if dotted not in self.modules:
            base = self.root.joinpath(*dotted.split("."))
            path = next((p for p in (base.with_suffix(".py"), base / "__init__.py") if p.is_file()), None)
            self.modules[dotted] = Module(dotted, path) if path and dotted.split(".")[0] in SUITE else None
        return self.modules[dotted]

    def resolve(self, dotted: str, name: str) -> Found | str | None:
        """What ``name`` means in a module: a definition, a module's dotted name, or nothing of this checkout."""

        module = self.module(dotted)
        if module is None:
            return None
        if name in module.defined:
            return dotted, module.defined[name]
        if name not in module.imported:
            return None
        source, attribute = module.imported[name]
        if attribute is None or self.module(f"{source}.{attribute}"):
            return source if attribute is None else f"{source}.{attribute}"
        return self.resolve(source, attribute)

    def member(self, dotted: str, cls: ast.ClassDef, name: str) -> Found | None:
        """A method or class attribute, looked up through the bases this checkout defines."""

        for node in cls.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return dotted, node
            if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == name for t in node.targets):
                return dotted, node.value
        for base in cls.bases:
            found = self.resolve(dotted, base.id) if isinstance(base, ast.Name) else None
            if isinstance(found, tuple) and isinstance(found[1], ast.ClassDef):
                inherited = self.member(found[0], found[1], name)
                if inherited:
                    return inherited
        return None

    def definition(self, found: Found, cls: ast.ClassDef | None = None) -> bool:
        """Whether a definition walks; a class is its constructor. A cycle counts as no walk."""

        dotted, node = found
        if isinstance(node, ast.ClassDef):
            init = self.member(dotted, node, "__init__")
            return bool(init) and self.definition(init, node)
        if id(node) not in self.known:
            self.known[id(node)] = False
            self.known[id(node)] = self.reaches(dotted, node, cls)
        return self.known[id(node)]

    def reaches(self, dotted: str, node: ast.AST, cls: ast.ClassDef | None) -> bool:
        return any(self.step(dotted, sub, cls) for sub in ast.walk(node))

    def step(self, dotted: str, node: ast.AST, cls: ast.ClassDef | None) -> bool:
        """Whether one node walks, or names something that does."""

        if is_walk(node):
            return True
        found: Found | str | None = None
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            found = self.resolve(dotted, node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if cls and node.value.id in ("self", "cls"):
                found = self.member(dotted, cls, node.attr)
            elif isinstance(owner := self.resolve(dotted, node.value.id), str):
                found = self.resolve(owner, node.attr)
        return isinstance(found, tuple) and self.definition(found, cls)

    def units(self, dotted: str) -> list[Unit]:
        """Every test method of a test module, with the tags Django gives it."""

        module = self.module(dotted)
        assert module, dotted
        found = []
        for cls in (node for node in module.defined.values() if isinstance(node, ast.ClassDef)):
            held = self.class_tags(dotted, cls)
            setup = [m for name in SETUP if (m := self.member(dotted, cls, name))]
            ready = any(self.definition(m, cls) for m in setup)
            for method in cls.body:
                if isinstance(method, ast.FunctionDef) and method.name.startswith("test"):
                    own = tags(method.decorator_list)
                    walks = ready or self.definition((dotted, method), cls)
                    label = f"{dotted}.{cls.name}.{method.name}"
                    found.append(Unit(label, frozenset(held | own), bool(own), walks))
        return found

    def class_tags(self, dotted: str, cls: ast.ClassDef) -> set[str]:
        held = tags(cls.decorator_list)
        for base in cls.bases:
            found = self.resolve(dotted, base.id) if isinstance(base, ast.Name) else None
            if isinstance(found, tuple) and isinstance(found[1], ast.ClassDef):
                held |= self.class_tags(found[0], found[1])
        return held


def test_modules(root: Path) -> list[str]:
    paths = [path for package in SUITE for path in source_files(root / package, "test*.py")]
    return [".".join(path.relative_to(root).with_suffix("").parts) for path in paths]


def misplaced(root: Path) -> tuple[list[str], list[str]]:
    """(source tests that walk without ``tree``, tree tags on no walk).

    A tag on a class asks only that one of its tests walk, so a class keeps
    one tag rather than one per method.
    """

    walks = Walks(root)
    untagged, idle = [], []
    for dotted in test_modules(root):
        by_class: dict[str, list[Unit]] = {}
        for unit in walks.units(dotted):
            by_class.setdefault(unit.label.rpartition(".")[0], []).append(unit)
            if unit.walks and "source" in unit.tags and "tree" not in unit.tags:
                untagged.append(unit.label)
        for cls, units in by_class.items():
            idle.extend(unit.label for unit in units if unit.on_method and "tree" in unit.tags and not unit.walks)
            if all("tree" in unit.tags and not unit.walks for unit in units) and any(not u.on_method for u in units):
                idle.append(cls)
    return untagged, idle


HELPER = """
from pathlib import Path


def every_file(root):
    return sorted(Path(root).rglob("*.py"))


def listed():
    return ["git", "ls-files"]
"""


@tag("source", "tree")
class TreeTagTests(SimpleTestCase):
    def test_a_source_test_that_walks_a_tree_carries_the_tree_tag(self):
        self.assertEqual(misplaced(ROOT)[0], [], "add tag 'tree': `mise run fast` runs these on every module change")

    def test_a_tree_tag_marks_a_test_that_walks(self):
        self.assertEqual(misplaced(ROOT)[1], [], "drop tag 'tree': nothing here walks a tree")

    def test_a_walk_is_found_through_a_helper_of_another_module(self):
        tests = """
            import os
            from django.test import SimpleTestCase, tag
            from tests.pkg import helper
            from tests.pkg.helper import listed


            @tag("source")
            class Direct(SimpleTestCase):
                def test_walks(self):
                    os.walk(".")

                def test_reads_one_file(self):
                    open("a")


            @tag("source")
            class Through(SimpleTestCase):
                def setUp(self):
                    self.files = helper.every_file(".")

                def test_anything(self):
                    pass


            class Methods(SimpleTestCase):
                @tag("source")
                def test_by_import(self):
                    listed()

                @tag("source", "tree")
                def test_tagged_without_a_walk(self):
                    pass


            @tag("source", "tree")
            class Tagged(SimpleTestCase):
                def test_walks(self):
                    self.walker()

                def walker(self):
                    return list(Path(".").rglob("*"))
        """
        with TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "tests" / "pkg"
            package.mkdir(parents=True)
            for name, text in (("__init__.py", ""), ("helper.py", HELPER), ("test_case.py", dedent(tests))):
                (package / name).write_text(text, encoding="utf-8")
            (root / "tests" / "__init__.py").write_text("")
            untagged, idle = misplaced(root)

        self.assertEqual(
            sorted(untagged),
            [
                "tests.pkg.test_case.Direct.test_walks",
                "tests.pkg.test_case.Methods.test_by_import",
                "tests.pkg.test_case.Through.test_anything",
            ],
        )
        self.assertEqual(idle, ["tests.pkg.test_case.Methods.test_tagged_without_a_walk"])
