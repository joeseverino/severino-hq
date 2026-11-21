"""The one way a validator refuses a value: by a ``Reason`` declared once.

A refusal travels further than the request did (logs, the audit trail,
idempotency records, agent transcripts), and the caller already holds what it
sent, so a refusal never echoes caller input. That is why ``Reason`` exists: its
words are declared text, which may name the field's constraint and never what
was sent, so ``input_errors`` says them whole. A validator is a ``Checked``
field marker or a ``checked`` model validator, and ``declared_only`` refuses any
other as an adapter HQ reads input with is built, so a validator that says its
own words does not load.

A leaf importing nothing of HQ and no validation library at load, so a leaf
contract (``automation_manifest``) declares its rules too.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import TYPE_CHECKING, Any, Literal, cast

lazy from pydantic import AfterValidator, BeforeValidator, ValidationError, WrapValidator
lazy from pydantic_core import PydanticCustomError

if TYPE_CHECKING:
    from pydantic import GetCoreSchemaHandler
    from pydantic_core import CoreSchema

_KIND = re.compile(r"[a-z][a-z0-9_]*")
# Each declared kind's words, which ``input_errors`` says for an error of that kind.
_DECLARED: dict[str, str] = {}
# The mark every validator ``Checked`` or ``checked`` makes carries.
_JUDGED = "__hq_judged__"


@dataclass(frozen=True, slots=True)
class Reason:
    """Why a value is refused: its error's ``kind``, what a client keys on, and
    the ``words`` that follow the field's name (``Reason("host_name", "must be
    a host name")``). The words name the constraint, never what was sent, so
    they take no template: a ``{`` is refused as it is declared, and so is a
    kind declared again in other words."""

    kind: str
    words: str

    def __post_init__(self) -> None:
        if not _KIND.fullmatch(self.kind):
            raise TypeError(f"A reason's kind is a snake_case name, never {self.kind!r}.")
        if not self.words or "{" in self.words:
            raise TypeError(f"Reason {self.kind!r} says what its field must be, in plain words with no template.")
        if _DECLARED.setdefault(self.kind, self.words) != self.words:
            raise TypeError(f"Reason {self.kind!r} is declared once; it already says {_DECLARED[self.kind]!r}.")

    def refused(self) -> Exception:
        """What a ``Checked`` or ``checked`` validator raises to refuse a value for this reason."""

        return _Refused(self)


class _Refused(Exception):
    """A value refused for a reason. Not a ``ValueError``, so only a validator
    ``Checked`` or ``checked`` makes, which turns it into its error, refuses by one."""

    def __init__(self, reason: Reason) -> None:
        super().__init__(reason.kind)
        self.reason = reason


def declared_words(kind: str) -> str | None:
    """The words the reason of ``kind`` says, or ``None`` for a kind no reason declares."""

    return _DECLARED.get(kind)


def _judging[F: Callable[..., Any]](check: F, reason: Reason | None) -> F:
    """``check``, refusing only by a reason: its own ``Reason``'s, or ``reason``
    for a ``ValueError`` or ``AssertionError``, which is a defect without one."""

    @wraps(check)
    def judged(*args: Any, **kwargs: Any) -> Any:
        try:
            return check(*args, **kwargs)
        except _Refused as refused:
            raise PydanticCustomError(refused.reason.kind, refused.reason.words) from None
        except ValidationError:
            # Another adapter's refusal of a part, held to declared reasons as this one is.
            raise
        except (ValueError, AssertionError) as error:
            if reason is None:
                raise TypeError(
                    f"{judged.__qualname__} refused in words of its own; raise a Reason's refused()."
                ) from error
            raise PydanticCustomError(reason.kind, reason.words) from None

    setattr(judged, _JUDGED, True)
    # ``wraps`` keeps the signature Pydantic reads to tell whether it takes its info.
    return cast("F", judged)


@dataclass(frozen=True, slots=True)
class Checked:
    """A field's rule: ``check`` takes the value (and Pydantic's info, where it
    names a second parameter) and returns it, changed or not. A ``ValueError``
    or ``AssertionError`` it raises is refused for ``reason``, and is a defect
    where it names none: ``Annotated[str, Checked(_pattern, Reason("pattern",
    "must be a regular expression"))]``. ``mode`` is Pydantic's: ``before``
    sees the value as sent, and ``wrap`` is handed the inner validation."""

    check: Callable[..., Any]
    reason: Reason | None = None
    mode: Literal["after", "before", "wrap"] = "after"

    def __get_pydantic_core_schema__(self, source: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        validator = {"after": AfterValidator, "before": BeforeValidator, "wrap": WrapValidator}[self.mode]
        return validator(_judging(self.check, self.reason)).__get_pydantic_core_schema__(source, handler)


def checked[F: Callable[..., Any]](validator: F) -> F:
    """A model's own validator, beneath Pydantic's ``model_validator`` or
    ``field_validator``, that refuses only by raising a reason's ``refused()``."""

    return _judging(validator, None)


def declared_only(schema: CoreSchema, where: str) -> None:
    """Refuse a core schema with a validator that says its own words: each is
    HQ's ``Checked`` or ``checked``, or Pydantic's own."""

    pending: list[Any] = [schema]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            if str(node.get("type", "")).startswith("function-") and isinstance(node.get("function"), dict):
                _refuse_unjudged(node["function"]["function"], where)
            # A serializer refuses nothing, and a class's schema is in the node beside it.
            pending.extend(value for key, value in node.items() if key not in {"serialization", "cls", "metadata"})
        elif isinstance(node, list | tuple):
            pending.extend(node)


def _refuse_unjudged(function: Any, where: str) -> None:
    # A bound method's own function carries the mark.
    if getattr(getattr(function, "__func__", function), _JUDGED, False):
        return
    if str(getattr(function, "__module__", "")).split(".")[0] in {"pydantic", "pydantic_core"}:
        return
    raise TypeError(
        f"{where} is validated by {getattr(function, '__qualname__', function)!r}, which refuses in words of its "
        "own; declare its rule as Checked(check, Reason(...)), or checked beneath Pydantic's decorator."
    )
