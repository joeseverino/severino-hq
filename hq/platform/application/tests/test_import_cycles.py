"""No module of HQ imports itself back while it is still being executed.

An import that runs at module level and leads back to its own module hands
that module out half-built, and whether that fails depends on which module
was imported first. Imports that wait (a `lazy` import, one inside a function,
one under `TYPE_CHECKING`) cannot close such a loop, so they are left out.
"""

import tempfile
from graphlib import CycleError, TopologicalSorter
from pathlib import Path

from django.test import SimpleTestCase, tag

from .facts import ROOT, Import, imports, module_name, package_of

PACKAGES = ("hq", "hq_sdk")
# What keeps an import from running as its module does.
WAITING = {"lazy", "called", "typed"}


def executed(found: list[Import]) -> list[Import]:
    """The imports a module runs as it executes: never a function's, a lazy one or a type checker's."""

    return [each for each in found if not each.waits & WAITING]


def import_graph(root: Path = ROOT, packages: tuple[str, ...] = PACKAGES) -> dict[str, set[str]]:
    """Each module, and the modules of this repository it imports as it runs.

    A module's own packages are already being imported when it runs, so an
    edge to one of them closes nothing."""

    root = root.resolve()
    modules = {module_name(path, root) for package in packages for path in (root / package).glob("**/*.py")}
    graph: dict[str, set[str]] = {module: set() for module in modules}
    for each in executed(imports(*(root / package for package in packages))):
        module = module_name(each.path, root)
        for name in each.modules(package_of(each.path, root)):
            parts = name.split(".")
            for depth in range(1, len(parts) + 1):
                target = ".".join(parts[:depth])
                if target in modules and target != module and not module.startswith(f"{target}."):
                    graph[module].add(target)
    return graph


def cycle_in(graph: dict[str, set[str]]) -> list[str]:
    try:
        TopologicalSorter(graph).prepare()
    except CycleError as error:
        return list(error.args[1])
    return []


@tag("source", "tree")
class ImportCycleTests(SimpleTestCase):
    def test_no_module_imports_itself_back_while_it_runs(self):
        cycle = cycle_in(import_graph())
        self.assertFalse(cycle, " -> ".join(cycle))

    def test_a_lazy_import_closes_no_loop_and_an_eager_one_does(self):
        eager = {"example.a": {"example.b"}, "example.b": {"example.a"}}
        self.assertTrue(cycle_in(eager))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "example").mkdir()
            (root / "example" / "__init__.py").write_text("")
            (root / "example" / "a.py").write_text("lazy from example.b import g\nfrom example.c import h\n")
            (root / "example" / "b.py").write_text("from example.a import f\n")
            (root / "example" / "c.py").write_text("from . import a\n")

            self.assertEqual(set(cycle_in(import_graph(root, ("example",)))), {"example.a", "example.c"})

    def test_an_import_that_waits_is_not_run_with_the_module(self):
        source = (
            "if TYPE_CHECKING:\n    import example.typed\nelse:\n    import example.otherwise\n"
            "def f():\n    import example.called\n"
            "try:\n    import example.tried\nexcept ImportError:\n    import example.fallback\n"
            "class C:\n    import example.in_class\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".py") as module:
            module.write(source)
            module.flush()
            names = [each.names[0][0] for each in executed(imports(module.name))]

        self.assertEqual(names, ["example.otherwise", "example.tried", "example.fallback", "example.in_class"])
