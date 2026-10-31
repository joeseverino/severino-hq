"""What a refusal may say, read from the source: the checker ``RefusalTextTests`` runs.

Every ``HostRefusal``, and every class that derives from it however deep, is
built from one sentence. A sentence is:

- a string literal;
- an f-string whose every value HQ wrote: a sentence, a number constant, one
  of ``FORMATTERS`` named from the module that declares it, a Django choice
  label (``get_<field>_display()``), or one of those with its case changed;
- a module constant bound to a sentence, in its own module or imported by name;
- a parameter or a frozen class's field annotated ``Sentence``, since every
  call and construction is held to pass one there;
- a local name every binding of which in its function is a sentence, or the
  ``Sentence`` part of what a sentence-returning function answers;
- a call to a function whose every definition under ``hq`` is annotated to
  return a ``Sentence``, each ``return`` of which is held to one;
- ``a or b``, ``a if c else b`` and ``a + b`` of sentences.

A formatter coerces what it is given (a date, a number) into HQ's form, so its
argument may come from anywhere. A function is found by its name, so a method
of an object from outside ``hq`` that shares a sentence function's name would
pass: those names are chosen to be HQ's own, and ``dataclasses.replace`` is
read only for the fields it names. A record's key is left out: by name,
``resource.key`` and the request's ``command.key`` read alike.
"""

import ast
import re
from collections import defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

ROOT = "HostRefusal"
MARKER = "Sentence"
# HQ's formatters: each turns a value into HQ's own words (a day, a count, a
# size), whatever the value was, so long as it is the one its module declares.
FORMATTERS = {
    "when_day": "hq/platform/application/moments.py",
    "counted": "hq/platform/application/ui.py",
    "human_bytes": "hq/platform/application/labels.py",
}
# The formatters whose arguments after the first are the words for the count.
WORDED = {"counted"}
CASE = {"lower", "capitalize"}
CHOICE_LABEL = re.compile(r"get_\w+_display")
# What builds a pydantic model from data rather than from its constructor.
BUILDERS = {"model_validate", "model_validate_json", "model_validate_strings", "model_construct", "model_copy"}

Function = ast.FunctionDef | ast.AsyncFunctionDef
SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


@dataclass(frozen=True)
class Place:
    """Where an expression is read: its module, and the class and function around it."""

    path: str
    cls: ast.ClassDef | None = None
    func: Function | None = None


def called(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def _marked(annotation: ast.expr | None) -> bool:
    return isinstance(annotation, ast.Name) and annotation.id == MARKER


def _sentence_position(annotation: ast.expr | None) -> int | bool | None:
    """Where a return annotation holds a sentence: ``None`` for the whole, an index in a tuple, or False."""

    if _marked(annotation):
        return None
    if isinstance(annotation, ast.Subscript) and isinstance(annotation.slice, ast.Tuple):
        for index, part in enumerate(annotation.slice.elts):
            if _marked(part):
                return index
    return False


def _module(path: str) -> list[str]:
    parts = path.removesuffix(".py").split("/")
    return parts[:-1] if parts[-1] == "__init__" else parts


def _imported_from(path: str, node: ast.ImportFrom) -> str:
    """The module an import names, as a dotted name."""

    if not node.level:
        return node.module or ""
    package = path.split("/")[:-1]
    package = package[: len(package) - node.level + 1]
    return ".".join([*package, *(node.module.split(".") if node.module else [])])


def _module_path(dotted: str, sources: Mapping[str, ast.Module]) -> str:
    base = dotted.replace(".", "/")
    return next((p for p in (f"{base}.py", f"{base}/__init__.py") if p in sources), f"{base}.py")


def _dataclass(node: ast.ClassDef) -> bool:
    return any(_base_name(d.func if isinstance(d, ast.Call) else d) == "dataclass" for d in node.decorator_list)


def _frozen(node: ast.ClassDef) -> bool:
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Call) and called(decorator) == "dataclass":
            return any(
                k.arg == "frozen" and isinstance(k.value, ast.Constant) and k.value.value for k in decorator.keywords
            )
    return any(
        isinstance(statement, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "model_config" for t in statement.targets)
        and isinstance(statement.value, ast.Call)
        and any(
            k.arg == "frozen" and isinstance(k.value, ast.Constant) and k.value.value for k in statement.value.keywords
        )
        for statement in node.body
    )


@dataclass
class Index:
    """What the whole tree declares, read once: refusals, sentence carriers and constants."""

    sources: Mapping[str, ast.Module]
    refusals: set[str] = field(default_factory=set)
    classes: dict[str, ast.ClassDef] = field(default_factory=dict)
    imports: dict[str, dict[str, tuple[str, str]]] = field(default_factory=lambda: defaultdict(dict))
    constants: dict[str, dict[str, ast.expr]] = field(default_factory=lambda: defaultdict(dict))
    returns: dict[str, list[int | bool | None]] = field(default_factory=lambda: defaultdict(list))
    params: dict[str, list[tuple[int | None, str]]] = field(default_factory=lambda: defaultdict(list))
    fields: dict[str, tuple[list[str], set[str]]] = field(default_factory=dict)
    defined: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))

    def __post_init__(self) -> None:
        for path, tree in self.sources.items():
            self._read_module(path, tree)
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    self._read_class(node)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self._read_function(node)
        self.refusals = self._derived(ROOT)

    def _read_module(self, path: str, tree: ast.Module) -> None:
        seen: dict[str, int] = defaultdict(int)
        for statement in tree.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.defined[path].add(statement.name)
            if isinstance(statement, ast.ImportFrom):
                for alias in statement.names:
                    self.imports[path][alias.asname or alias.name] = (_imported_from(path, statement), alias.name)
            for target, value in _assigned(statement):
                if isinstance(target, ast.Name):
                    seen[target.id] += 1
                    self.constants[path][target.id] = value
        for name, count in seen.items():
            if count > 1:
                del self.constants[path][name]

    def _read_class(self, node: ast.ClassDef) -> None:
        self.classes.setdefault(node.name, node)
        order, marked = [], set()
        for statement in node.body:
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                order.append(statement.target.id)
                if _marked(statement.annotation):
                    marked.add(statement.target.id)
        if marked:
            self.classes[node.name] = node
            self.fields[node.name] = (order, marked)

    def _read_function(self, node: Function) -> None:
        self.returns[node.name].append(_sentence_position(node.returns))
        if node.name.startswith("__"):
            # A refusal's own constructor is read as the refusal; every other
            # class shares the name.
            return
        positional = [*node.args.posonlyargs, *node.args.args]
        if positional and positional[0].arg in {"self", "cls"}:
            positional = positional[1:]
        for index, argument in enumerate(positional):
            if _marked(argument.annotation):
                self.params[node.name].append((index, argument.arg))
        for argument in node.args.kwonlyargs:
            if _marked(argument.annotation):
                self.params[node.name].append((None, argument.arg))

    def _derived(self, root: str) -> set[str]:
        found = {root}
        while True:
            more = {
                name
                for name, node in self.classes.items()
                if name not in found and any(_base_name(base) in found for base in node.bases)
            }
            if not more:
                return found
            found |= more

    def returns_sentence_at(self, name: str) -> int | bool | None:
        """Where every definition of ``name`` returns its sentence, or False when one does not."""

        positions = set(self.returns.get(name, ()))
        if len(positions) != 1:
            return False
        return positions.pop()

    def class_fields(self, cls: ast.ClassDef | None) -> set[str]:
        marked: set[str] = set()
        while cls is not None:
            marked |= self.fields.get(cls.name, ([], set()))[1]
            parent = next((self.classes.get(_base_name(b)) for b in cls.bases if _base_name(b) in self.classes), None)
            cls = parent
        return marked


def _assigned(statement: ast.stmt) -> list[tuple[ast.expr, ast.expr]]:
    if isinstance(statement, ast.Assign):
        return [(target, statement.value) for target in statement.targets]
    if isinstance(statement, ast.AnnAssign) and statement.value is not None:
        return [(statement.target, statement.value)]
    return []


def _base_name(base: ast.expr) -> str:
    if isinstance(base, ast.Attribute):
        return base.attr
    return base.id if isinstance(base, ast.Name) else ""


def _own_nodes(func: Function) -> Iterator[ast.AST]:
    """The nodes of a function's body, outside the functions and classes it declares."""

    pending: list[ast.AST] = list(func.body)
    while pending:
        node = pending.pop()
        yield node
        if not isinstance(node, SCOPES):
            pending.extend(ast.iter_child_nodes(node))


def _bindings(func: Function, name: str) -> Iterator[tuple[ast.expr | None, int | None]]:
    """Each value ``name`` is bound to in ``func``, with the tuple position it takes; ``None`` for an unknown one."""

    for node in _own_nodes(func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                yield from _bound(target, node.value, name)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
            yield from _bound(node.target, node.value, name)
        elif name in _other_names(node):
            yield None, None


def _other_names(node: ast.AST) -> set[str]:
    """The names a loop, a ``with``, a walrus, an ``except`` or an import binds."""

    if isinstance(node, (ast.For, ast.AsyncFor, ast.NamedExpr)):
        target: ast.AST | None = node.target
    elif isinstance(node, ast.withitem):
        target = node.optional_vars
    elif isinstance(node, ast.ExceptHandler):
        return {node.name} if node.name else set()
    elif isinstance(node, ast.alias):
        return {node.asname or node.name.split(".")[0]}
    else:
        return set()
    return {n.id for n in ast.walk(target) if isinstance(n, ast.Name)} if target is not None else set()


def _bound(target: ast.expr, value: ast.expr, name: str) -> Iterator[tuple[ast.expr | None, int | None]]:
    if isinstance(target, ast.Name) and target.id == name:
        yield value, None
    elif isinstance(target, (ast.Tuple, ast.List)):
        for index, part in enumerate(target.elts):
            if isinstance(part, ast.Name) and part.id == name:
                yield value, index
            elif isinstance(part, ast.Starred) and name in {n.id for n in ast.walk(part) if isinstance(n, ast.Name)}:
                yield None, None


class Reader:
    """Whether an expression, read where it stands, is a sentence."""

    def __init__(self, index: Index) -> None:
        self.index = index

    def sentence(self, expr: ast.expr | None, place: Place) -> bool:
        match expr:
            case ast.Constant(value=str()):
                return True
            case ast.JoinedStr(values=values):
                return all(self.written(v.value, place) for v in values if isinstance(v, ast.FormattedValue))
            case ast.BinOp(op=ast.Add(), left=left, right=right):
                return self.sentence(left, place) and self.sentence(right, place)
            case ast.BoolOp(values=values):
                return all(self.sentence(v, place) for v in values)
            case ast.IfExp(body=body, orelse=orelse):
                return self.sentence(body, place) and self.sentence(orelse, place)
            case ast.Name(id=name):
                return self._name(name, place)
            case ast.Attribute(value=ast.Name(id="self"), attr=attr):
                return attr in self.index.class_fields(place.cls)
            case ast.Call():
                return self.index.returns_sentence_at(called(expr)) is None
        return False

    def written(self, expr: ast.expr, place: Place) -> bool:
        """A value HQ wrote, as an f-string may interpolate it."""

        if self.sentence(expr, place) or (isinstance(expr, ast.Constant) and isinstance(expr.value, int)):
            return True
        if isinstance(expr, ast.Name):
            found = self._constant(expr.id, place)
            return found is not None and isinstance(found[0], ast.Constant) and isinstance(found[0].value, int)
        if not isinstance(expr, ast.Call):
            return False
        name = called(expr)
        if isinstance(expr.func, ast.Attribute) and not expr.args and not expr.keywords:
            return bool(CHOICE_LABEL.fullmatch(name)) or (name in CASE and self.written(expr.func.value, place))
        return self._formatted(expr, place)

    def _formatted(self, call: ast.Call, place: Place) -> bool:
        if not isinstance(call.func, ast.Name) or call.func.id not in FORMATTERS:
            return False
        home = FORMATTERS[call.func.id]
        origin = self.index.imports.get(place.path, {}).get(call.func.id)
        if origin is None or call.func.id in self.index.defined.get(place.path, ()):
            declared = place.path == home
        else:
            declared = _module_path(origin[0], self.index.sources) == home
        words = [*call.args[1:], *(k.value for k in call.keywords)] if call.func.id in WORDED else []
        return declared and all(isinstance(w, ast.Constant) and isinstance(w.value, str) for w in words)

    def _name(self, name: str, place: Place) -> bool:
        func = place.func
        if func is not None:
            arguments = [*func.args.posonlyargs, *func.args.args, *func.args.kwonlyargs]
            argument = next((a for a in arguments if a.arg == name), None)
            bindings = list(_bindings(func, name))
            if argument is not None or bindings:
                own = argument is None or _marked(argument.annotation)
                return own and all(self.binding(value, at, place) for value, at in bindings)
        found = self._constant(name, place)
        return found is not None and self.sentence(*found)

    def binding(self, value: ast.expr | None, at: int | None, place: Place) -> bool:
        if at is None:
            return self.sentence(value, place)
        if isinstance(value, ast.Tuple) and at < len(value.elts):
            return self.sentence(value.elts[at], place)
        return isinstance(value, ast.Call) and self.index.returns_sentence_at(called(value)) == at

    def _constant(self, name: str, place: Place) -> tuple[ast.expr, Place] | None:
        """A module constant ``name`` reads where it stands, and the module it is declared in."""

        if name in self.index.constants.get(place.path, {}):
            return self.index.constants[place.path][name], Place(place.path)
        origin = self.index.imports.get(place.path, {}).get(name)
        if origin is None:
            return None
        home = _module_path(origin[0], self.index.sources)
        value = self.index.constants.get(home, {}).get(origin[1])
        return None if value is None else (value, Place(home))


def places(path: str, tree: ast.Module) -> Iterator[tuple[ast.AST, Place]]:
    """Every node of a module, with the class and function it is read in."""

    def walk(node: ast.AST, place: Place) -> Iterator[tuple[ast.AST, Place]]:
        for child in ast.iter_child_nodes(node):
            yield child, place
            if isinstance(child, ast.ClassDef):
                yield from walk(child, Place(path, child, None))
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield from walk(child, Place(path, place.cls, child))
            else:
                yield from walk(child, place)

    yield from walk(tree, Place(path))


class Checker:
    """Every place a sentence is promised, each held to one; ``problems`` says where one is not."""

    def __init__(self, sources: Mapping[str, ast.Module]) -> None:
        self.index = Index(sources)
        self.reader = Reader(self.index)
        self.problems: list[str] = []
        self.refusals_built = 0
        self.carried = 0
        for path, tree in sources.items():
            for node, place in places(path, tree):
                self._check(node, place)

    def _fail(self, place: Place, node: ast.AST, what: str) -> None:
        self.problems.append(f"{place.path}:{getattr(node, 'lineno', 0)} {what}")

    def _check(self, node: ast.AST, place: Place) -> None:
        if isinstance(node, ast.Call):
            self._call(node, place)
        elif isinstance(node, ast.ClassDef):
            self._class(node, place)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._function(node, place)
        elif isinstance(node, ast.alias) and node.asname and self._carrier(node.name):
            self._fail(place, node, f"renames {node.name}, so its uses are not read as its")

    def _carrier(self, name: str) -> bool:
        index = self.index
        return (
            name in index.refusals
            or name in index.fields
            or name in index.params
            or name == MARKER
            or index.returns_sentence_at(name) is not False
        )

    def _call(self, call: ast.Call, place: Place) -> None:
        name = called(call)
        if name in self.index.refusals:
            self._refusal(call, place)
        if name in self.index.params:
            self._arguments(call, place, self.index.params[name])
        if name in self.index.fields:
            order, marked = self.index.fields[name]
            self._arguments(call, place, [(order.index(f), f) for f in marked])
        if name == "replace":
            marked = set().union(*(m for c, (_, m) in self.index.fields.items() if _dataclass(self.index.classes[c])))
            named = [k for k in call.keywords if k.arg in marked]
            if any(not self.reader.sentence(k.value, place) for k in named):
                self._fail(place, call, "replaces a sentence with something else")
        func = call.func
        if name in BUILDERS and isinstance(func, ast.Attribute) and _base_name(func.value) in self.index.fields:
            self._fail(place, call, f"builds {_base_name(func.value)} from data, past its sentence fields")

    def _refusal(self, call: ast.Call, place: Place) -> None:
        self.refusals_built += 1
        given = [*call.args, *(k.value for k in call.keywords if k.arg == "reason")]
        whole = len(given) == 1 and len(call.args) + len(call.keywords) == 1
        if not (whole and self.reader.sentence(given[0], place)):
            self._fail(place, call, f"builds its {called(call)} from something other than a sentence")

    def _arguments(self, call: ast.Call, place: Place, slots: list[tuple[int | None, str]]) -> None:
        if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
            self._fail(place, call, f"unpacks into {called(call)}, which takes a sentence")
            return
        by_name = {k.arg: k.value for k in call.keywords}
        for position, slot in slots:
            value = by_name.get(slot)
            if value is None and position is not None and position < len(call.args):
                value = call.args[position]
            if value is not None:
                self.carried += 1
                if not self.reader.sentence(value, place):
                    self._fail(place, call, f"passes {called(call)} a {slot} that is not a sentence")

    def _class(self, node: ast.ClassDef, place: Place) -> None:
        constructors = [
            s for s in node.body if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) and s.name == "__init__"
        ]
        if node.name in self.index.refusals and node.name != ROOT and constructors:
            self._fail(place, node, f"{node.name} takes its reason its own way")
        if node.name not in self.index.fields:
            return
        if not self._frozen(node):
            self._fail(place, node, f"{node.name} holds a sentence and can be changed after it is built")
        defaults = [
            s for s in node.body if isinstance(s, ast.AnnAssign) and _marked(s.annotation) and s.value is not None
        ]
        for statement in defaults:
            if not self.reader.sentence(statement.value, Place(place.path, node)):
                self._fail(place, statement, f"{node.name} defaults a sentence to something else")

    def _frozen(self, node: ast.ClassDef | None) -> bool:
        while node is not None:
            if _frozen(node):
                return True
            node = next(
                (self.index.classes.get(_base_name(b)) for b in node.bases if _base_name(b) in self.index.classes), None
            )
        return False

    def _function(self, func: Function, place: Place) -> None:
        inner = Place(place.path, place.cls, func)
        arguments = [*func.args.posonlyargs, *func.args.args]
        defaults = zip(arguments[len(arguments) - len(func.args.defaults) :], func.args.defaults, strict=True)
        keyword_defaults = zip(func.args.kwonlyargs, func.args.kw_defaults, strict=True)
        for argument, default in [*defaults, *keyword_defaults]:
            if default is not None and _marked(argument.annotation) and not self.reader.sentence(default, place):
                self._fail(place, func, f"{func.name} defaults its sentence {argument.arg} to something else")
        position = _sentence_position(func.returns)
        if position is False:
            return
        for node in _own_nodes(func):
            if isinstance(node, ast.Return) and not self.reader.binding(node.value, position, inner):
                self._fail(place, node, f"{func.name} returns something other than a sentence")


def check(sources: Mapping[str, ast.Module]) -> Checker:
    return Checker(sources)
