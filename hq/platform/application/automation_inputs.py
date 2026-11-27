"""A playbook's inputs as HQ adopts them: each its roles' argument spec
declares, held to a closed type and its sidecar's bound, and the model a
run's parameters are validated with."""

import json
import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationError,
    create_model,
)

from .automation_manifest import AUTHORED
from .automation_sources import InputBound
from .command_fields import Describe
from .contracts import Closed

# The names an input may have, as a sidecar's ``inputs`` keys them.
INPUT_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")

# Ansible's own types, as an argument spec names them, that a closed input
# may be; ``type`` left out is a string, as Ansible reads it. The free-form
# ones (``dict``, ``raw``, ``json``, ``jsonarg``) and the ones a run has no
# use taking (``bits``, ``bytes``, ``sid``) are refused.
SCALARS = {"str": StrictStr, "path": StrictStr, "int": StrictInt, "float": StrictFloat, "bool": StrictBool}
TEXT = frozenset({"str", "path"})
type Scalar = Literal["str", "path", "int", "float", "bool"]


class AdoptedInput(Closed):
    """One input of a playbook, as adopted: its argument spec's terms, and
    the sidecar's limit on a text input."""

    model_config = ConfigDict(frozen=True, strict=True)

    type: Literal["str", "path", "int", "float", "bool", "list"]
    elements: Scalar | None = None
    required: bool = False
    default: (
        StrictStr | StrictInt | StrictFloat | StrictBool | tuple[StrictStr | StrictInt | StrictFloat, ...] | None
    ) = None
    choices: tuple[StrictStr | StrictInt | StrictFloat, ...] = ()
    description: Annotated[str, StringConstraints(max_length=1000), AUTHORED] = ""
    bound: InputBound | None = None


def _scalar(kind: str, choices: tuple[Any, ...], bound: InputBound | None) -> Any:
    if choices:
        return Literal[choices]
    if kind in TEXT:
        if bound is None:
            raise ValueError("a text input needs its choices or a bound")
        return Annotated[StrictStr, StringConstraints(pattern=bound.pattern, max_length=bound.max_length)]
    return SCALARS[kind]


class _Inputs(Closed):
    """A procedure's parameters, fixed once read."""

    model_config = ConfigDict(frozen=True)


def input_type(operation_id: str, inputs: Mapping[str, AdoptedInput]) -> type[BaseModel]:
    """The closed model a procedure's parameters are validated with, from its
    inputs as adopted: each by its Ansible type, its choices or its bound."""

    fields: dict[str, Any] = {}
    for name, spec in inputs.items():
        if spec.type == "list":
            if spec.elements is None:
                raise ValueError(f"the list input {name} names no type for its elements")
            annotation: Any = tuple[_scalar(spec.elements, spec.choices, spec.bound), ...]
        else:
            annotation = _scalar(spec.type, spec.choices, spec.bound)
        described = Annotated[annotation, Describe(spec.description)] if spec.description else annotation
        if spec.required:
            fields[name] = (described, ...)
        elif spec.default is None:
            fields[name] = (described | None, None)
        else:
            fields[name] = (described, Field(default=spec.default, validate_default=True))
    title = "".join(part.title() for part in operation_id.replace(".", "_").split("_"))
    model: type[BaseModel] = create_model(f"{title}Inputs", __base__=_Inputs, __module__=__name__, **fields)
    return model


def _description(value: Any) -> str:
    text = " ".join(value) if isinstance(value, list) else value if isinstance(value, str) else ""
    return text[:1000]


def procedure_inputs(found: Mapping[str, Any], bounds: Mapping[str, InputBound], refused: list[str]) -> dict:
    """The inputs every role of one procedure declares, each once, bounded as
    its sidecar says, with each problem among them added to ``refused``."""

    options: dict[str, Mapping[str, Any]] = {}
    for role in found["roles"]:
        spec = role.get("argument_spec") or {}
        for option, terms in (spec.get("options") or {}).items():
            if option in options and options[option] != terms:
                refused.append(f"Two of its roles declare the input {option} differently.")
            options.setdefault(option, terms if isinstance(terms, Mapping) else {})
    refused.extend(
        f"Its sidecar limits the input {unknown}, which none of its roles takes."
        for unknown in sorted(set(bounds) - set(options))
    )
    inputs: dict[str, AdoptedInput] = {}
    for option, terms in sorted(options.items()):
        problem = _input(option, terms, bounds.get(option), inputs)
        if problem:
            refused.append(problem)
    return inputs


def _input(option: str, terms: Mapping[str, Any], bound: InputBound | None, inputs: dict[str, AdoptedInput]) -> str:
    """Adds ``option`` to ``inputs`` as Ansible's terms and its sidecar's
    ``bound`` say, or answers why it cannot run."""

    kind = terms.get("type", "str")
    if not INPUT_NAME.fullmatch(option):
        return f"The input name {option} isn't allowed; use lowercase letters, digits and underscores."
    if kind not in (*SCALARS, "list") or terms.get("elements") not in (None, *SCALARS):
        return f"The input {option} takes {kind}, a free-form type; name each value it allows."
    # A run's command is published with each input's words, and a model
    # filling one in has only them to go by.
    if not _description(terms.get("description")).strip():
        return f"The input {option} says nothing of what it is; give it a description in its argument_specs."
    try:
        adopted = AdoptedInput.model_validate_json(
            json.dumps(
                {
                    "type": kind,
                    "elements": terms.get("elements"),
                    "required": bool(terms.get("required", False)),
                    "default": terms.get("default"),
                    "choices": terms.get("choices") or [],
                    "description": _description(terms.get("description")),
                    "bound": bound.model_dump() if bound is not None else None,
                }
            )
        )
    except ValidationError as exc:
        return f"The input {option} cannot be read ({exc.errors(include_url=False)[0]['msg']})."
    inputs[option] = adopted
    text = adopted.elements if kind == "list" else kind
    if text in TEXT and not adopted.choices and bound is None:
        return f"The text input {option} needs its choices, or a pattern and length in its sidecar."
    return ""
