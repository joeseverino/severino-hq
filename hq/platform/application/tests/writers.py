"""Which functions in this checkout write, and which views call one.

A function writes when its body writes a model (an instance's ``save`` or
``delete``, a manager's or queryset's bulk write), calls the service a view
declares (``self.service``, which a form mixin's contract makes a mutation),
or calls a function that does, followed through every name a module defines
or imports from ``hq``. Read from the source, so a writer added later is found
without anybody listing it.
"""

import ast
import secrets
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from django.http import HttpResponse

from .. import plugin_testing
from ..web_writes import RETRY_KEY_FIELD
from .markup import all_elements
from .source import parsed, source_files

# The chokepoint as a browser's form reaches it, under the key the form
# posted, and the runner beneath it that a door carrying a key of its own
# (a token's header, a logout token's id) calls: a view reaches every domain
# write through them, so none runs a write without its key.
CHOKEPOINTS = frozenset(
    {
        "hq.platform.application.web_writes.execute_posted",
        "hq.platform.application.web_writes.run_posted",
        "hq.platform.application.capabilities.run_capability",
    }
)

# Rows that record who called, not what they changed: an audit event, a
# refusal (an audit event, coalesced) and an identity observation (when an
# agent was last seen). None is a domain write, so a function reaching only
# these does not write, and anything may reach them.
# A chokepoint handed the write itself, as a lambda it runs under the posted
# key, and the one module whose lambdas it keys: the form mixins, whose write
# is a declared service rather than a capability. A view handing it a write of
# its own is still a write outside the capability chokepoint.
KEYED_LAMBDAS = {"hq.platform.application.web_writes.write_once": "hq.platform.application.writes"}

# What a call to a view's declared service resolves to.
DECLARED_SERVICE = "a declared service"

RECORDS_OF_CALLERS = frozenset(
    {
        "hq.platform.core.audit.record_event",
        "hq.platform.application.denials.record_denial",
        "hq.platform.application.denials.record_limited",
        "hq.platform.application.denials.settle",
        "hq.platform.application.agent_registry.observe",
    }
)

_INSTANCE_WRITES = frozenset({"save", "delete"})
_BULK_WRITES = frozenset({"create", "get_or_create", "update_or_create", "bulk_create", "bulk_update", "update"})


@dataclass
class _Function:
    name: str
    module: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    owner: str = ""
    calls: set[str] = field(default_factory=set)
    # What it calls inside a lambda a keyed chokepoint runs, under the posted key.
    keyed_calls: set[str] = field(default_factory=set)
    writes: bool = False


def _module_name(root: Path, path: Path, packages: Sequence[Path] = ()) -> str:
    """The name ``path`` imports as: from the installed package that holds it, else from ``root``."""

    base = next((package.parent for package in packages if path.is_relative_to(package)), root)
    parts = path.relative_to(base).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _imports(module: str, tree: ast.Module, *, is_package: bool) -> dict[str, str]:
    """Every name the module binds by import, to the dotted name it binds."""

    bound: dict[str, str] = {}
    package = module if is_package else module.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.split(".")
                anchor = anchor[: len(anchor) - (node.level - 1)]
                base = ".".join([*anchor, *([base] if base else [])])
            for alias in node.names:
                bound[alias.asname or alias.name] = f"{base}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bound[alias.asname] = alias.name
                else:
                    head = alias.name.split(".")[0]
                    bound[head] = head
    return bound


def _is_bulk_receiver(node: ast.expr) -> bool:
    """Whether a bulk write's receiver is a manager or a queryset built from one."""

    while isinstance(node, (ast.Call, ast.Attribute)):
        if isinstance(node, ast.Attribute) and node.attr == "objects":
            return True
        node = node.func if isinstance(node, ast.Call) else node.value
    return False


def _writes_directly(call: ast.Call) -> bool:
    target = call.func
    if not isinstance(target, ast.Attribute):
        return False
    if target.attr in _INSTANCE_WRITES:
        return True
    return target.attr in _BULK_WRITES and _is_bulk_receiver(target.value)


def _called(call: ast.Call, *, module: str, owner: str, bound: dict[str, str], local: set[str]) -> str:
    """The dotted name a call resolves to, or "" for one this cannot follow."""

    target = call.func
    if isinstance(target, ast.Name):
        if target.id in local:
            return f"{module}.{target.id}"
        return bound.get(target.id, "")
    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
        receiver = target.value.id
        if receiver in {"self", "cls"} and owner:
            return f"{module}.{owner}.{target.attr}"
        if receiver in bound:
            return f"{bound[receiver]}.{target.attr}"
        if receiver in local:
            return f"{module}.{receiver}.{target.attr}"
    return ""


@dataclass
class _Module:
    tree: ast.Module
    bound: dict[str, str]
    defines: set[str]


def _modules(root: Path, also: Sequence[Path], packages: Sequence[Path]) -> dict[str, _Module]:
    """Every module under ``hq`` and ``hq_sdk``, in each of ``packages`` (an installed
    extension's whole source), and in ``also``: views routed from outside them."""

    trees = [root / "hq", root / "hq_sdk", *packages]
    walked = {
        path
        for tree in trees
        for path in source_files(tree, "*.py")
        if not {"tests", "migrations"} & set(path.relative_to(tree).parts)
    }
    found = {}
    for path in sorted({*walked, *also}):
        module = _module_name(root, path, packages)
        tree = parsed(path)
        local = {
            node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        }
        found[module] = _Module(tree, _imports(module, tree, is_package=path.name == "__init__.py"), local)
    return found


def _canonical(name: str, modules: dict[str, _Module]) -> str:
    """``name`` where it is defined, through every module that only re-exports it (``hq_sdk``'s)."""

    seen = set()
    while name and name not in seen:
        seen.add(name)
        parts = name.split(".")
        for split in range(len(parts) - 1, 0, -1):
            entry = modules.get(".".join(parts[:split]))
            head = parts[split]
            if entry is not None and head in entry.bound and head not in entry.defines:
                name = ".".join([entry.bound[head], *parts[split + 1 :]])
                break
        else:
            return name
    return name


def _bases(node: ast.ClassDef, module: str, entry: _Module, modules: dict[str, _Module]) -> set[str]:
    """The dotted name of each base ``node`` names, where it is defined."""

    named = set()
    for base in node.bases:
        if isinstance(base, ast.Name):
            named.add(f"{module}.{base.id}" if base.id in entry.defines else entry.bound.get(base.id, ""))
        elif isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name) and base.value.id in entry.bound:
            named.add(f"{entry.bound[base.value.id]}.{base.attr}")
    return {_canonical(name, modules) for name in named - {""}}


def _functions(modules: dict[str, _Module], classes: dict[str, set[str]]) -> Iterator[_Function]:
    """Every function the modules define, at their top level or in a class, and each class's bases into ``classes``."""

    for module, entry in modules.items():
        for node in entry.tree.body:
            if isinstance(node, ast.ClassDef):
                classes[f"{module}.{node.name}"] = _bases(node, module, entry, modules)
            members = [(node.name, child) for child in node.body] if isinstance(node, ast.ClassDef) else [("", node)]
            for owner, child in members:
                if not isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                found = _Function(child.name, module, child, owner)
                called = partial(_called, module=module, owner=owner, bound=entry.bound, local=entry.defines)

                def resolve(call: ast.Call, called: Callable[[ast.Call], str] = called) -> str:
                    return _canonical(called(call), modules)

                for call, under_key in _runs(child, resolve, module):
                    name = resolve(call)
                    if under_key:
                        found.keyed_calls.update({name} - {""})
                        continue
                    found.writes = found.writes or _writes_directly(call)
                    if name:
                        found.calls.add(name)
                yield found


def _runs(
    node: ast.FunctionDef | ast.AsyncFunctionDef, resolve: Callable[[ast.Call], str], module: str
) -> Iterator[tuple[ast.Call, bool]]:
    """The calls running ``node`` makes, each with whether it runs under a posted key.

    A function it defines counts once it uses it, and not when it only hands
    the function back (a factory). A lambda ``module`` hands a chokepoint in
    ``KEYED_LAMBDAS`` runs under that call's key.
    """

    returned = {
        statement.value.id
        for statement in ast.walk(node)
        if isinstance(statement, ast.Return) and isinstance(statement.value, ast.Name)
    }
    used = {
        name.id for name in ast.walk(node) if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Load)
    } - returned
    pending: list[tuple[ast.AST, bool]] = [(statement, False) for statement in node.body]
    while pending:
        current, under_key = pending.pop()
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef) and current.name not in used:
            continue
        if isinstance(current, ast.Call):
            yield current, under_key
            if not under_key and KEYED_LAMBDAS.get(resolve(current)) == module:
                arguments = (*current.args, *(keyword.value for keyword in current.keywords))
                pending.append((current.func, False))
                pending.extend((argument, isinstance(argument, ast.Lambda)) for argument in arguments)
                continue
        pending.extend((child, under_key) for child in ast.iter_child_nodes(current))


def _dotted(function: _Function) -> str:
    return ".".join(part for part in (function.module, function.owner, function.name) if part)


def writers(
    root: Path, also: Sequence[Path] = (), packages: Sequence[Path] = ()
) -> tuple[dict[str, _Function], set[str], dict[str, set[str]]]:
    """Every function by dotted name, the names of those that make a domain write, and each class's bases."""

    classes: dict[str, set[str]] = {}
    functions = {_dotted(function): function for function in _functions(_modules(root, also, packages), classes)}
    for function in functions.values():
        declared = f"{function.module}.{function.owner}.service"
        if function.owner and declared in function.calls and declared not in functions:
            function.calls = (function.calls - {declared}) | {DECLARED_SERVICE}
    writing = {name for name, function in functions.items() if function.writes} - RECORDS_OF_CALLERS
    writing.add(DECLARED_SERVICE)
    grew = True
    while grew:
        grew = False
        for name, function in functions.items():
            if name not in writing and name not in RECORDS_OF_CALLERS and function.calls & writing:
                writing.add(name)
                grew = True
    return functions, writing, classes


@dataclass(frozen=True)
class ViewWrite:
    path: str
    function: str
    service: str


# What a view that writes a model in its own body is said to call.
DIRECT = "a model, directly"


def view_writes(root: Path, view_modules: list[Path], packages: Sequence[Path] = ()) -> list[ViewWrite]:
    """Each model write a view makes in its own body, and each call it makes into
    a domain writer defined outside the views, other than the chokepoint.

    Whatever the writer takes: a use case handed ``request.user`` writes as
    surely as one handed a principal, and only the chokepoint authorizes and
    audits the write. ``packages`` are the installed extensions whose views
    are among ``view_modules``, each read whole, as the host's ``hq`` is.
    """

    functions, writing, classes = writers(root, view_modules, packages)
    view_names = {_module_name(root, path, packages) for path in view_modules}
    keyed = _keyed(functions, classes)
    found = []
    for name, function in sorted(functions.items()):
        if function.module not in view_names or name in keyed:
            continue
        if function.writes:
            found.append(ViewWrite(function.module, name.removeprefix(f"{function.module}."), DIRECT))
        for service in sorted(function.calls & writing):
            if service in CHOKEPOINTS or KEYED_LAMBDAS.get(service) == function.module:
                continue
            if service in functions and functions[service].module in view_names and service not in keyed:
                continue
            found.append(ViewWrite(f"{function.module}", name.removeprefix(f"{function.module}."), service))
    return found


def _keyed(functions: dict[str, _Function], classes: dict[str, set[str]]) -> set[str]:
    """The functions that run under the posted key: those a keyed lambda calls,
    a subclass's override of one (a form view's ``run``), and what they call.

    One is still a write when a view calls it outside such a lambda, which
    ``view_writes`` reports at that caller.
    """

    keyed = {name for function in functions.values() for name in function.keyed_calls if name in functions}
    for name, function in functions.items():
        if function.owner and any(
            f"{base}.{function.name}" in keyed for base in _ancestors(f"{function.module}.{function.owner}", classes)
        ):
            keyed.add(name)
    pending = list(keyed)
    while pending:
        for called in functions[pending.pop()].calls:
            if called in functions and called not in keyed:
                keyed.add(called)
                pending.append(called)
    return keyed


def _ancestors(name: str, classes: dict[str, set[str]]) -> set[str]:
    found: set[str] = set()
    pending = list(classes.get(name, ()))
    while pending:
        base = pending.pop()
        if base not in found:
            found.add(base)
            pending.extend(classes.get(base, ()))
    return found


def keyed(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """``payload`` with a fresh retry key, as a token's change carries one, for a
    test whose subject is not the key (``run_capability`` refuses one without)."""

    return {**(payload or {}), "idempotency_key": f"example-{secrets.token_hex(8)}"}


# A test that posts a form it did not open: the host's post with the copy
# hq_sdk.testing gives the extensions'.
form_keyed = plugin_testing.form_keyed


def drawn_key(response: HttpResponse, attribute: str, value: str | None = None) -> str:
    """The retry key the page drew in its one write form carrying ``attribute``
    (equal to ``value`` when given), as a browser posts it."""

    (key,) = (
        field.attrs["value"]
        for field in all_elements(response)
        if field.tag == "input"
        and field.attrs.get("name") == RETRY_KEY_FIELD
        and any(
            form.tag == "form" and attribute in form.attrs and value in (None, form.attrs[attribute])
            for form in field.ancestors
        )
    )
    return key
