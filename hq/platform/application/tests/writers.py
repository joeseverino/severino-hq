"""Which functions in this checkout write, and which views call one.

A function writes when its body writes a model (an instance's ``save`` or
``delete``, a manager's or queryset's bulk write) or calls a function that
does, followed through every name a module defines or imports from ``hq``.
Read from the source, so a writer added later is found without anybody
listing it.
"""

import ast
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .source import parsed, source_files

# The chokepoint itself, and the idempotent run it makes of a machine write: a
# view reaches every domain write through them.
CHOKEPOINTS = frozenset(
    {
        "hq.platform.application.capabilities.execute_capability",
        "hq.platform.application.idempotency.execute_once",
    }
)

# Rows that record who called, not what they changed: an audit event, a
# refusal (an audit event, coalesced) and an identity observation (when an
# agent was last seen). None is a domain write, so a function reaching only
# these does not write, and anything may reach them.
RECORDS_OF_CALLERS = frozenset(
    {
        "hq.platform.core.audit.record_event",
        "hq.platform.application.denials.record_denial",
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
    writes: bool = False


def _module_name(root: Path, path: Path) -> str:
    parts = path.relative_to(root).with_suffix("").parts
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


def _functions(root: Path) -> Iterator[_Function]:
    for path in source_files(root / "hq", "*.py"):
        if "tests" in path.parts or "migrations" in path.parts:
            continue
        module = _module_name(root, path)
        tree = parsed(path)
        bound = _imports(module, tree, is_package=path.name == "__init__.py")
        local = {
            node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        }
        for node in tree.body:
            members = [(node.name, child) for child in node.body] if isinstance(node, ast.ClassDef) else [("", node)]
            for owner, child in members:
                if not isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                found = _Function(child.name, module, child, owner)
                for call in _runs(child):
                    found.writes = found.writes or _writes_directly(call)
                    name = _called(call, module=module, owner=owner, bound=bound, local=local)
                    if name:
                        found.calls.add(name)
                yield found


def _runs(node: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[ast.Call]:
    """The calls running ``node`` makes: a function it defines counts once it
    uses it, and not when it only hands the function back (a factory)."""

    returned = {
        statement.value.id
        for statement in ast.walk(node)
        if isinstance(statement, ast.Return) and isinstance(statement.value, ast.Name)
    }
    used = {
        name.id for name in ast.walk(node) if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Load)
    } - returned
    pending: list[ast.AST] = list(node.body)
    while pending:
        current = pending.pop()
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef) and current.name not in used:
            continue
        if isinstance(current, ast.Call):
            yield current
        pending.extend(ast.iter_child_nodes(current))


def _dotted(function: _Function) -> str:
    return ".".join(part for part in (function.module, function.owner, function.name) if part)


def writers(root: Path) -> tuple[dict[str, _Function], set[str]]:
    """Every function by dotted name, and the names of those that make a domain write."""

    functions = {_dotted(function): function for function in _functions(root)}
    writing = {name for name, function in functions.items() if function.writes} - RECORDS_OF_CALLERS
    grew = True
    while grew:
        grew = False
        for name, function in functions.items():
            if name not in writing and name not in RECORDS_OF_CALLERS and function.calls & writing:
                writing.add(name)
                grew = True
    return functions, writing


@dataclass(frozen=True)
class ViewWrite:
    path: str
    function: str
    service: str


def view_writes(root: Path, view_modules: list[Path]) -> list[ViewWrite]:
    """Each call a view makes into a domain writer defined outside the views, other than the chokepoint.

    Whatever the writer takes: a use case handed ``request.user`` writes as
    surely as one handed a principal, and only the chokepoint authorizes and
    audits the write.
    """

    functions, writing = writers(root)
    view_names = {_module_name(root, path) for path in view_modules}
    found = []
    for name, function in sorted(functions.items()):
        if function.module not in view_names:
            continue
        for service in sorted(function.calls & writing):
            if service in CHOKEPOINTS or functions[service].module in view_names:
                continue
            found.append(ViewWrite(f"{function.module}", name.removeprefix(f"{function.module}."), service))
    return found
