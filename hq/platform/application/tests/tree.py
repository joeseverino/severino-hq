"""Which tests of a checkout carry which tags, and which of them walk a tree or run a program the image lacks, read from their source.

The guard in test_tree_tag.py holds the ``tree`` tag to the walks, and
``mise run fast`` runs the tests it finds tagged ``tree``. The guard in
test_unshipped_tag.py holds a test that runs git or a tool mise pins to the
``source`` tag. Each walk
runs apart (apart.py), so the trees it parses leave with its process.
"""

import ast
import re
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from hq.platform.application.tests.apart import apart
lazy from hq.platform.application.tests.source import source_files

ROOT = Path(__file__).resolve().parents[4]


def packages(root: Path) -> tuple[str, ...]:
    """A checkout's top-level Python packages: each directory at its root that holds an ``__init__.py``."""

    return tuple(sorted(entry.name for entry in root.iterdir() if (entry / "__init__.py").is_file()))


# The packages the suite lives in, read from the tree so a new one joins every reader at once.
SUITE = packages(ROOT)
SETUP = ("setUp", "setUpClass", "setUpTestData")

# A definition found by name: the module it lives in and its node.
Found = tuple[str, ast.AST]


def is_walk(node: ast.AST) -> bool:
    """``x.rglob(...)``, ``os.walk(...)``, ``ls-files`` in a string, or a command line that runs ast-grep
    on files, which one that reads ``--stdin`` does not."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return "ls-files" in node.value.split()
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        words = [each.value for each in node.elts if isinstance(each, ast.Constant)]
        return words[:1] == ["ast-grep"] and "--stdin" not in words
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    owner = node.func.value
    return node.func.attr == "rglob" or (node.func.attr == "walk" and isinstance(owner, ast.Name) and owner.id == "os")


def unshipped(root: Path) -> frozenset[str]:
    """The programs a checkout runs that its image lacks: git, and each tool
    mise.toml pins (by its name, an ``aqua:`` one by its repository's), since
    the image ships Python and the base system only."""

    pinned = tomllib.loads((root / "mise.toml").read_text(encoding="utf-8")).get("tools", {})
    return frozenset({"git", *(name.rpartition("/")[2].rpartition(":")[2] for name in pinned)})


def runs(programs: frozenset[str]) -> Callable[[ast.AST], bool]:
    """Whether a node is a command line that runs one of ``programs``: a list or tuple whose first item names it."""

    def found(node: ast.AST) -> bool:
        if not (isinstance(node, (ast.List, ast.Tuple)) and node.elts):
            return False
        head = node.elts[0]
        return isinstance(head, ast.Constant) and head.value in programs

    return found


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
        # Parsed once per module by Reach, in a process that exits, so no cache keeps it.
        for node in ast.parse(path.read_text(encoding="utf-8"), filename=str(path)).body:
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
            head = _imported_from(node, package)
            for alias in node.names:
                self.imported[alias.asname or alias.name] = (head, alias.name)


def _imported_from(node: ast.ImportFrom, package: str) -> str:
    """The dotted name of the module a ``from`` import in ``package`` reads, a relative one resolved."""

    if not node.level:
        return node.module or ""
    base = package.split(".")[: len(package.split(".")) - node.level + 1]
    return ".".join([*base, *([node.module] if node.module else [])])


@dataclass(frozen=True)
class Unit:
    """One test method: its label, its tags and where its own tags stand."""

    label: str
    tags: frozenset[str]
    on_method: bool
    walks: bool


class Reach:
    """Which definitions of a checkout reach what ``found`` picks out (a walk
    unless told otherwise), followed by name across its modules."""

    def __init__(self, root: Path, found: Callable[[ast.AST], bool] = is_walk):
        self.root = root
        self.found = found
        self.modules: dict[str, Module | None] = {}
        self.known: dict[int, bool] = {}

    def module(self, dotted: str) -> Module | None:
        if dotted not in self.modules:
            base = self.root.joinpath(*dotted.split("."))
            path = next((p for p in (base.with_suffix(".py"), base / "__init__.py") if p.is_file()), None)
            self.modules[dotted] = Module(dotted, path) if path and dotted.split(".", maxsplit=1)[0] in SUITE else None
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
        """Whether one node is what is looked for, or names something that reaches it."""

        if self.found(node):
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
        """Every test method of a test module, with the tags Django gives it.

        A class that replaces the ROOT of the code it drives runs that code on
        a fixture, so what it walks is the fixture, never this checkout.
        """

        found = []
        for cls, method, held, own in self.methods(dotted):
            setup = [m for name in SETUP if (m := self.member(dotted, cls, name))]
            walks = not replaces_root([method, *(node for _, node in setup)]) and self.reached(dotted, cls, method)
            found.append(Unit(f"{dotted}.{cls.name}.{method.name}", frozenset(held | own), bool(own), walks))
        return found

    def reached(self, dotted: str, cls: ast.ClassDef, method: ast.FunctionDef) -> bool:
        """Whether a test method, or its class's setup, reaches what is looked for."""

        setup = [m for name in SETUP if (m := self.member(dotted, cls, name))]
        return any(self.definition(m, cls) for m in setup) or self.definition((dotted, method), cls)

    def methods(self, dotted: str) -> Iterator[tuple[ast.ClassDef, ast.FunctionDef, set[str], set[str]]]:
        """Each test method of a test module: its class, itself, its class's tags and its own."""

        module = self.module(dotted)
        assert module, dotted
        for cls in (node for node in module.defined.values() if isinstance(node, ast.ClassDef)):
            held = self.class_tags(dotted, cls)
            for method in cls.body:
                if isinstance(method, ast.FunctionDef) and method.name.startswith("test"):
                    yield cls, method, held, tags(method.decorator_list)

    def class_tags(self, dotted: str, cls: ast.ClassDef) -> set[str]:
        held = tags(cls.decorator_list)
        for base in cls.bases:
            found = self.resolve(dotted, base.id) if isinstance(base, ast.Name) else None
            if isinstance(found, tuple) and isinstance(found[1], ast.ClassDef):
                held |= self.class_tags(found[0], found[1])
        return held


def replaces_root(nodes: list[ast.AST]) -> bool:
    """Whether any of ``nodes`` patches a module's ROOT: ``patch.object(module, "ROOT", ...)`` or ``patch("a.b.ROOT", ...)``."""

    for node in (sub for each in nodes for sub in ast.walk(each)):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        name = getattr(node.func, "attr", getattr(node.func, "id", ""))
        named = [arg.value for arg in node.args[:2] if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]
        if (name == "object" and named == ["ROOT"]) or (name == "patch" and named[:1] and named[0].endswith(".ROOT")):
            return True
    return False


def tagged(root: Path, tag: str) -> list[str]:
    """The label of every test carrying ``tag``: its class's, when every test of the class does."""

    return apart(_tagged, root, tag)


def _tagged(root: str, tag: str) -> list[str]:
    walks = Reach(Path(root))
    labels = []
    for dotted in test_modules(walks.root):
        by_class: dict[str, list[tuple[str, bool]]] = {}
        for cls, method, held, own in walks.methods(dotted):
            by_class.setdefault(f"{dotted}.{cls.name}", []).append((method.name, tag in held | own))
        for cls, methods in by_class.items():
            if all(carries for _, carries in methods):
                labels.append(cls)
            else:
                labels += [f"{cls}.{name}" for name, carries in methods if carries]
    return labels


def test_modules(root: Path, packages: tuple[str, ...] = SUITE) -> list[str]:
    paths = [path for package in packages for path in source_files(root / package, "test*.py")]
    return [".".join(path.relative_to(root).with_suffix("").parts) for path in paths]


def misplaced(root: Path) -> tuple[list[str], list[str]]:
    """(source tests that walk without ``tree``, tree tags on no walk).

    A tag on a class asks only that one of its tests walk, so a class keeps
    one tag rather than one per method.
    """

    untagged, idle = apart(_misplaced, root)
    return untagged, idle


def _misplaced(root: str) -> tuple[list[str], list[str]]:
    walks = Reach(Path(root))
    untagged, idle = [], []
    for dotted in test_modules(walks.root):
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


def shipped(root: Path) -> tuple[str, ...]:
    """The packages of SUITE the image is built from: those Dockerfile.dockerignore admits."""

    ignore = (root / "Dockerfile.dockerignore").read_text(encoding="utf-8")
    admitted = set(re.findall(r"^!([^*/\s]+)/?$", ignore, re.MULTILINE))
    return tuple(package for package in SUITE if package in admitted)


def runs_unshipped_untagged(root: Path) -> list[str]:
    """Every test the image ships that runs a program the image lacks
    (``unshipped``), itself or through what it reaches by name, without the
    ``source`` tag."""

    return apart(_runs_unshipped_untagged, root)


def _runs_unshipped_untagged(root: str) -> list[str]:
    reach = Reach(Path(root), runs(unshipped(Path(root))))
    asks = Reach(Path(root), is_which)
    return [
        f"{dotted}.{cls.name}.{method.name}"
        for dotted in test_modules(reach.root, shipped(reach.root))
        for cls, method, held, own in reach.methods(dotted)
        if "source" not in held | own
        and reach.reached(dotted, cls, method)
        and not skips_without_a_program(asks, dotted, cls, method)
    ]


def is_which(node: ast.AST) -> bool:
    """A call of ``shutil.which``, which asks whether a program is installed."""

    return isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) == "which"


def skips_without_a_program(asks: Reach, dotted: str, cls: ast.ClassDef, method: ast.FunctionDef) -> bool:
    """Whether a test, or its class, is skipped by a ``skipIf`` or ``skipUnless``
    whose condition asks for a program (``asks`` finds ``shutil.which``), so an
    image without it skips the test rather than fails it."""

    return any(
        isinstance(decorator, ast.Call)
        and getattr(decorator.func, "attr", getattr(decorator.func, "id", "")) in ("skipIf", "skipUnless")
        and any(asks.reaches(dotted, condition, cls) for condition in decorator.args[:1])
        for decorator in [*cls.decorator_list, *method.decorator_list]
    )
