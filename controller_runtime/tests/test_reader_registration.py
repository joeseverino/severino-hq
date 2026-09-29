"""An architecture test: a reader reaches ``OBSERVATION_READERS`` one of two ways.

An integration's adapter declares its readers (``readings=``), admitted by
``_register_adapters``; a core reader in ``providers`` carries
``@reads("kind")`` on its own definition. Nothing else registers one.
"""

from __future__ import annotations

import ast
from pathlib import Path

from django.test import SimpleTestCase

from control_plane.observations import OBSERVATIONS
from control_plane.provider_adapters.contracts import CORE_PROBED_CONNECTIONS
from control_plane.provider_adapters import CONTROLLER_PROVIDER_ADAPTERS

from controller_runtime import handlers, provider_runtime, providers

ROOT = Path(__file__).parent.resolve().parent.parent
ADMITTING = "_register_adapters"
_MUTATORS = frozenset({"update", "setdefault", "pop", "popitem", "clear", "__setitem__"})


def _sources():
    for folder in (ROOT / "controller_runtime", ROOT / "control_plane"):
        for path in sorted(folder.rglob("*.py")):
            if not path.name.startswith("test") and "migrations" not in path.parts:
                yield path, ast.parse(path.read_text(encoding="utf-8"))


def _decorated_kinds(tree: ast.Module) -> list[str]:
    """Kinds named by ``@reads("kind")`` on module-level functions."""

    return [
        decorator.args[0].value
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Name)
        and decorator.func.id == "reads"
        and decorator.args
        and isinstance(decorator.args[0], ast.Constant)
    ]


def _registrations(path: Path, tree: ast.Module) -> list[str]:
    """Every use of ``reads`` or ``OBSERVATION_READERS`` that is not one of the two ways."""

    allowed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if node.name in ("reads", ADMITTING):
                allowed.update(id(inner) for inner in ast.walk(node))
            if node in tree.body:
                allowed.update(id(decorator) for decorator in node.decorator_list)
                allowed.update(
                    id(inner) for decorator in node.decorator_list for inner in ast.walk(decorator)
                )
    found = []
    for node in ast.walk(tree):
        if id(node) in allowed:
            continue
        called = isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "reads"
        written = (
            isinstance(node, (ast.Subscript, ast.Attribute))
            and ast.unparse(node.value).endswith("OBSERVATION_READERS")
            and (
                node.attr in _MUTATORS
                if isinstance(node, ast.Attribute)
                else isinstance(node.ctx, (ast.Store, ast.Del))
            )
        )
        if called or written:
            found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return found


class ReaderRegistrationTests(SimpleTestCase):
    def test_no_reader_is_registered_any_other_way(self):
        offenders = [line for path, tree in _sources() for line in _registrations(path, tree)]

        self.assertEqual(offenders, [])

    def test_the_registered_readers_are_exactly_the_adapters_and_the_decorated_core(self):
        decorated = [kind for _path, tree in _sources() for kind in _decorated_kinds(tree)]
        declared = provider_runtime._ADAPTER_REGISTRY.readings

        self.assertEqual(len(decorated), len(set(decorated)))
        self.assertFalse(set(decorated) & set(declared))
        self.assertEqual(set(handlers.OBSERVATION_READERS), set(decorated) | set(declared))
        for kind, reader in declared.items():
            with self.subTest(kind=kind):
                self.assertIs(handlers.OBSERVATION_READERS[kind], reader)

    def test_a_reading_through_an_integrations_connection_is_declared_by_its_adapter(self):
        held = {
            provider
            for adapter in CONTROLLER_PROVIDER_ADAPTERS
            for provider in (
                *adapter.reads_through,
                *(p for d in adapter.definitions for p in d.connection_providers),
            )
        } - CORE_PROBED_CONNECTIONS
        decorated = [kind for _path, tree in _sources() for kind in _decorated_kinds(tree)]

        self.assertEqual(
            [kind for kind in decorated if OBSERVATIONS[kind].provider in held], []
        )

    def test_the_check_sees_a_reader_registered_by_hand(self):
        tree = ast.parse(
            "from x import reads, OBSERVATION_READERS\n"
            "@reads('example.fine')\n"
            "def fine():\n    return []\n"
            "for kind, reader in {}.items():\n    reads(kind)(reader)\n"
            "OBSERVATION_READERS['example.thing'] = fine\n"
            "OBSERVATION_READERS.update({})\n"
            "OBSERVATION_READERS.get('example.fine')\n"
        )

        self.assertEqual(len(_registrations(ROOT / "example.py", tree)), 3)
        self.assertEqual(_decorated_kinds(tree), ["example.fine"])


class AdapterLayerTests(SimpleTestCase):
    def test_nothing_in_the_control_plane_imports_the_controller_runtime(self):
        """Adapters are admitted by the web process too, so they reach the
        controller only through ``ProviderRuntime``."""

        offenders = []
        for path, tree in _sources():
            if "control_plane" not in path.relative_to(ROOT).parts[:1]:
                continue
            for node in ast.walk(tree):
                names = (
                    [alias.name for alias in node.names] if isinstance(node, ast.Import)
                    else [node.module or ""] if isinstance(node, ast.ImportFrom) and not node.level
                    else []
                )
                if any(name.split(".")[0] == "controller_runtime" for name in names):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")

        self.assertEqual(offenders, [])


class DispatchIsTheRegistriesTests(SimpleTestCase):
    """The dispatch tables are what handlers registered, and nothing else.

    A handler written into a table by hand is a second list:
    it can name a kind the control plane does not declare, or an action it
    locks, and nothing beside the definition says so.
    """

    def test_every_table_is_its_registry(self):
        from .. import handlers

        locked = {
            identity
            for identity, handler in providers.PROVIDER_ACTIONS.items()
            if getattr(handler, "__name__", "") == "locked"
        }
        self.assertEqual(set(providers.PROVIDER_ACTIONS) - locked, set(handlers.ACTIONS))
        self.assertEqual(
            set(providers.PROVIDER_INVENTORY),
            set(handlers.INVENTORY) | set(handlers.OBSERVATION_READERS),
        )
        self.assertIs(providers._CONNECTION_PROBES, handlers.PROBES)

    def test_nothing_writes_a_registry_but_its_decorator(self):
        tables = ("ACTIONS", "INVENTORY", "PROBES", "PROVIDER_ACTIONS", "PROVIDER_INVENTORY")
        offenders = []
        for path, tree in _sources():
            if path.name == "handlers.py":
                continue
            for node in ast.walk(tree):
                target = node.value if isinstance(node, (ast.Subscript, ast.Attribute)) else None
                if target is None or not ast.unparse(target).endswith(tables):
                    continue
                if isinstance(node, ast.Attribute) and node.attr in _MUTATORS:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
                elif isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")

        self.assertEqual(offenders, [])
