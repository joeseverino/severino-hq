"""The shape of everything ``hq_sdk`` exports, stated once and committed.

Every module here re-exports host objects by name, so a test that the names
resolve proves nothing about what an extension binds to: a dataclass field
renamed, a parameter added without a default, a method that went away. Each of
those is a fleet-wide change, and this repository's suite and code graph both
report it as safe, because every caller lives in another repository.

``describe()`` walks each export and records its shape -- the parameters a
callable takes, the fields a dataclass or model carries, the members of an
enum, the public methods a class defines -- and deliberately not annotations or
docstrings, so the record is identical across the interpreter matrix and moves
only when the contract does. ``contract.json`` beside this module is the shape
extensions were built against. A test fails when the two differ, the diff is
the review, and whether ``PLUGIN_API_VERSION`` must move is the question that
review answers.

    python manage.py sdk_contract          # rewrite contract.json
    python manage.py sdk_contract --check  # exit 1 on drift
"""

from __future__ import annotations

import dataclasses
import enum
import importlib
import inspect
import json
import pkgutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel

import hq_sdk

CONTRACT_PATH = Path(__file__).with_name("contract.json")

def module_names() -> tuple[str, ...]:
    """Every SDK module, discovered rather than listed, so none can be missed."""

    return tuple(
        sorted(
            info.name
            for info in pkgutil.iter_modules(hq_sdk.__path__)
            if info.name != "contract"
        )
    )


def exports(module) -> tuple[str, ...]:
    """What a module offers: its ``__all__``, or what it defines itself."""

    declared = getattr(module, "__all__", None)
    if declared is not None:
        return tuple(declared)
    return tuple(
        sorted(
            name
            for name, value in vars(module).items()
            if not name.startswith("_")
            and getattr(value, "__module__", None) == module.__name__
        )
    )


def _parameters(target) -> list[str] | None:
    """A signature as short strings: ``name``, ``name=`` when it has a default,
    ``*args``/``**kwargs``, and the bare ``*`` and ``/`` markers Python uses."""

    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return None
    rendered: list[str] = []
    keyword_only_started = False
    for parameter in signature.parameters.values():
        kind = parameter.kind
        if kind is inspect.Parameter.KEYWORD_ONLY and not keyword_only_started:
            rendered.append("*")
            keyword_only_started = True
        if kind is inspect.Parameter.VAR_POSITIONAL:
            rendered.append(f"*{parameter.name}")
            keyword_only_started = True
            continue
        if kind is inspect.Parameter.VAR_KEYWORD:
            rendered.append(f"**{parameter.name}")
            continue
        optional = parameter.default is not inspect.Parameter.empty
        rendered.append(f"{parameter.name}=" if optional else parameter.name)
        if kind is inspect.Parameter.POSITIONAL_ONLY:
            following = list(signature.parameters.values())
            index = following.index(parameter)
            if index + 1 == len(following) or following[index + 1].kind is not kind:
                rendered.append("/")
    return rendered


def _members(cls) -> dict[str, Any]:
    """What the class itself defines, not what it inherits."""

    members: dict[str, Any] = {}
    for name, value in vars(cls).items():
        if name.startswith("_"):
            continue
        if isinstance(value, property):
            members[name] = {"kind": "property"}
        elif isinstance(value, (staticmethod, classmethod)):
            members[name] = {
                "kind": type(value).__name__,
                "parameters": _parameters(getattr(cls, name)),
            }
        elif inspect.isfunction(value):
            members[name] = {"kind": "method", "parameters": _parameters(value)}
    return members


def _class(cls) -> dict[str, Any]:
    if issubclass(cls, enum.Enum):
        return {"kind": "enum", "members": [member.name for member in cls]}
    shape: dict[str, Any] = {
        "kind": "class",
        "bases": [base.__qualname__ for base in cls.__bases__ if base is not object],
        "constructor": _parameters(cls),
        "members": _members(cls),
    }
    if dataclasses.is_dataclass(cls):
        # The constructor already carries every init field, in order, with its
        # default; only the fields a caller cannot pass need naming here.
        shape["kind"] = "dataclass"
        shape["fields"] = sorted(
            field.name for field in dataclasses.fields(cls) if not field.init
        )
    elif issubclass(cls, BaseModel):
        shape["kind"] = "model"
        shape["fields"] = {
            name: {"required": info.is_required()}
            for name, info in cls.model_fields.items()
        }
    return shape


def _shape(value) -> dict[str, Any]:
    if inspect.isclass(value):
        return _class(value)
    if inspect.isroutine(value):
        return {"kind": "function", "parameters": _parameters(value)}
    return {"kind": "value", "type": type(value).__name__}


def describe() -> dict[str, Any]:
    """The current contract, as plain JSON data."""

    from application.plugins import PLUGIN_API_VERSION

    modules = {}
    for name in module_names():
        module = importlib.import_module(f"hq_sdk.{name}")
        modules[name] = {export: _shape(getattr(module, export)) for export in exports(module)}
    contract = {
        "api_version": PLUGIN_API_VERSION,
        "sdk_version": hq_sdk.SDK_VERSION,
        "modules": modules,
    }
    return json.loads(json.dumps(contract, sort_keys=True))


def render(contract: dict[str, Any]) -> str:
    return json.dumps(contract, indent=2, sort_keys=True) + "\n"


def load_committed() -> dict[str, Any]:
    if not CONTRACT_PATH.exists():
        return {}
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _summary(before: dict[str, Any], after: dict[str, Any]) -> str:
    parts = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        if isinstance(old, dict) and isinstance(new, dict):
            added = sorted(set(new) - set(old))
            removed = sorted(set(old) - set(new))
            changed = sorted(name for name in set(old) & set(new) if old[name] != new[name])
            detail = ", ".join(
                item
                for item in (
                    f"+{','.join(added)}" if added else "",
                    f"-{','.join(removed)}" if removed else "",
                    f"~{','.join(changed)}" if changed else "",
                )
                if item
            )
            parts.append(f"{key}: {detail}")
        else:
            parts.append(f"{key}: {json.dumps(old)} -> {json.dumps(new)}")
    return "; ".join(parts)


def drift(committed: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Every difference between two contracts, one line each, empty when none."""

    lines = []
    for key in ("api_version", "sdk_version"):
        if committed.get(key) != current.get(key):
            lines.append(f"{key}: {committed.get(key)!r} -> {current.get(key)!r}")
    before = committed.get("modules", {})
    after = current.get("modules", {})
    for module in sorted(set(before) | set(after)):
        if module not in before:
            lines.append(f"+ hq_sdk.{module}: {', '.join(sorted(after[module]))}")
            continue
        if module not in after:
            lines.append(f"- hq_sdk.{module}")
            continue
        for name in sorted(set(before[module]) | set(after[module])):
            if name not in before[module]:
                lines.append(f"+ hq_sdk.{module}.{name}")
            elif name not in after[module]:
                lines.append(f"- hq_sdk.{module}.{name}")
            elif before[module][name] != after[module][name]:
                lines.append(
                    f"~ hq_sdk.{module}.{name}: "
                    f"{_summary(before[module][name], after[module][name])}"
                )
    return lines
