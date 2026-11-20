"""The one refusal contract: the envelope every code (``refusal_codes.Code``) is answered in, and how a
failure becomes one.

``refusal_of`` is the one place an exception becomes a refusal, so the one
place an authorization refusal is audited.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, NotRequired, TypedDict, TypeIs

from django.core.exceptions import ValidationError as DjangoValidationError
from pydantic import Field, ValidationError as PydanticValidationError

from hq.platform.core.errors import UpstreamUnavailable

from .answer_fields import Outcome, Pinned
from .input_errors import django_refusal, pydantic_refusal
from .projection import QueryRefusal
from .record_errors import HostRefusal
from .refusal_codes import Code, Door
from .security import AuthorizationError, InsufficientScope, Principal
lazy from .approvals import RefusedHold
lazy from .denials import record_denial
lazy from .integration_specs import CapabilitySpec, takes_stale_write_token
lazy from .refusal_pointers import RefusalPointers
lazy from .resources import InvalidResourceInput, ResourceError

logger = logging.getLogger("severino.capabilities")


# The codes a missing grant answers: the caller's own, or its token's.
WANTS_A_GRANT = frozenset({Code.FORBIDDEN, Code.INSUFFICIENT_SCOPE})

# A change's doors: a read takes no retry key and is never held.
_WRITE_DOORS = (Door.RETRY_KEY, Door.HOLD, Door.DECISION)


def capability_doors(spec: CapabilitySpec | None) -> tuple[Door, ...]:
    """The doors a call of ``spec`` passes, from what it declares; every capability's for None, a call by name."""

    if spec is None:
        return (
            Door.CAPABILITY,
            Door.NAME,
            Door.TARGETED,
            Door.UNTARGETED,
            Door.STALE_WRITE,
            *_WRITE_DOORS,
        )
    return call_doors(
        targeted=bool(spec.target_kind),
        stale_write=takes_stale_write_token(spec.handler),
        changes=spec.effect.changes,
    )


def call_doors(*, targeted: bool, stale_write: bool, changes: bool) -> tuple[Door, ...]:
    """The doors a call of a capability declared so passes: each kind of call the API document names."""

    doors = [Door.CAPABILITY, Door.TARGETED if targeted else Door.UNTARGETED]
    if stale_write:
        doors.append(Door.STALE_WRITE)
    if changes:
        doors.extend(_WRITE_DOORS)
    return tuple(doors)


def _loggable(value: object) -> str:
    """``value`` as part of one log line, whatever the request put in it: its repr, with no line break."""

    return repr(value).replace("\n", "\\n")


# Each part of a refusal, a named alias, so every refusal the API document
# narrows (its codes, whether it waits) says each part once.
type RefusalCode = Annotated[
    str, Field(description="Which cause, from HQ's table of codes (docs/API.md, Errors). Branch on the status.")
]
type RefusalMessage = Annotated[
    str, Field(description="HQ's sentence for the refusal, naming no value the request carried.")
]


class NextCall(TypedDict):
    """A call that gets past a refusal: an MCP tool and its arguments, which
    the machine API serves as the route of that name."""

    tool: Annotated[str, Field(description="The MCP tool's name.")]
    arguments: Annotated[dict[str, Any], Field(description="The tool's arguments, as its input schema names them.")]


class InputProblem(TypedDict):
    """One field that was wrong and why, as Pydantic reports it."""

    type: Annotated[str, Field(description="Pydantic's error type.")]
    loc: Annotated[list[str | int], Field(description="Where in the request it is.")]
    msg: Annotated[str, Field(description="Pydantic's sentence for it.")]


class RecordProblem(TypedDict):
    """One record of a document HQ refused, and why."""

    errors: Annotated[list[str], Field(description="Each problem the record has.")]
    doc_id: NotRequired[Annotated[str, Field(description="The doc it is, for a vault sync.")]]
    kind: NotRequired[Annotated[str, Field(description="The kind of record it is.")]]
    index: NotRequired[Annotated[int, Field(description="Its place among the records of its kind, from 0.")]]
    slug: NotRequired[Annotated[str, Field(description="The slug it names.")]]


type RefusalDetails = Annotated[
    RefusalPointers
    | Annotated[list[InputProblem], Field(description="For `invalid_input`, each field that was wrong and why.")]
    | Annotated[
        dict[str, list[str]],
        Field(description="For `invalid_input` or `not_found`, each field a record refused, with its error codes."),
    ]
    | Annotated[list[RecordProblem], Field(description="Each record of a document refused.")],
    Field(description="What gets past the refusal, where something does."),
]
type RetryAfter = Annotated[
    int, Field(ge=1, description="Whole seconds until asking again can succeed; the `Retry-After` header repeats it.")
]


# The envelope every door answers a refusal in, typed once: the API document
# and the MCP tools' output schema both derive theirs from it.
class ErrorDetail(TypedDict):
    code: RefusalCode
    message: RefusalMessage
    details: NotRequired[RefusalDetails]
    retry_after: NotRequired[RetryAfter]


class RefusalEnvelope(TypedDict):
    ok: Annotated[Outcome, Pinned(value=False)]
    error: Annotated[ErrorDetail, Field(description="Why HQ refused, and what gets past it.")]


@dataclass(frozen=True, slots=True)
class Refusal:
    """One refusal, as every adapter answers it: HQ's code and words, and what goes with them."""

    code: Code
    message: str
    details: RefusalDetails | None = None
    # Seconds until asking again can succeed, for a refusal that says "later".
    retry_after: int | None = None

    def __post_init__(self) -> None:
        # The API document declares the wait only where a code says "later".
        if not self.code.waits:
            object.__setattr__(self, "retry_after", None)

    @property
    def status(self) -> int:
        return self.code.status

    @property
    def scope(self) -> str:
        """The permission an ``insufficient_scope`` refusal names, which a door's challenge repeats; "" for any other."""

        if self.code != Code.INSUFFICIENT_SCOPE or not isinstance(self.details, Mapping):
            return ""
        return str(self.details.get("scope", ""))

    def answer(self) -> dict[str, Any]:
        """The ``{"ok": false}`` envelope every adapter carries."""

        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            error["details"] = self.details
        if self.retry_after is not None:
            error["retry_after"] = self.retry_after
        return {"ok": False, "error": error}


def refuse(code: Code, message: str, details: RefusalDetails | None = None) -> dict[str, Any]:
    """A refusal's envelope, built where the refusal is decided."""

    return Refusal(code, message, details).answer()


def refused(answer: Mapping[str, object]) -> TypeIs[RefusalEnvelope]:
    """Whether a capability's answer is its refusal, so the rest of it reads as the success it is."""

    return answer.get("ok") is False


def next_call(tool: str, **arguments: Any) -> NextCall:
    """The call that gets past a refusal, as a refusal's ``details.next`` carries it:
    an MCP tool and its arguments, which the machine API serves as the route of that name."""

    return {"tool": tool, "arguments": arguments}


def refusal_of(outcome: dict[str, Any] | Exception, *, name: str = "", principal: Principal | None = None) -> Refusal:
    """The refusal ``outcome`` stands for: a ``{"ok": false}`` answer, or an exception raised running ``name``.

    Given the ``principal`` it refused, an authorization refusal or a held call
    that could not wait is recorded once, here. ``Principal.require`` records
    nothing, since ``permits`` asks it to decide what to draw.
    """

    if isinstance(outcome, dict):
        error = outcome.get("error") or {}
        code = error.get("code", Code.OPERATION_FAILED)
        if code in Code:
            return Refusal(
                Code(code),
                error.get("message", f"{name or 'This request'} could not be executed."),
                error.get("details"),
                error.get("retry_after"),
            )
        # A plugin's handler answers its code as text. One the table does not
        # hold is a defect in that plugin, and a client can key on no code it
        # was never told of, so it is answered as the failure it is. The code
        # stays in the log.
        logger.error("Refusal code %s has no status.", _loggable(code), extra={"event": "refusal.unlisted"})
        return Refusal(Code.OPERATION_FAILED, "This request could not be executed.")
    refusal = _mapped(outcome, name)
    if principal is not None and isinstance(outcome, (AuthorizationError, RefusedHold)):
        record_denial(
            interface=principal.interface,
            actor=principal.actor,
            capability=name,
            reason=refusal.code,
            device=principal.device,
        )
    return refusal


def _mapped(exc: Exception, name: str) -> Refusal:
    if isinstance(exc, PydanticValidationError):
        refused = pydantic_refusal(name, exc.errors())
        return Refusal(Code.INVALID_INPUT, refused.message, refused.details)
    if isinstance(exc, DjangoValidationError):
        refused = django_refusal(name, exc)
        # Only fields naming records that are not there: the call names a missing record.
        codes = [code for named in refused.details.values() for code in named]
        missing = bool(codes) and all(code == Code.NOT_FOUND for code in codes)
        return Refusal(Code.NOT_FOUND if missing else Code.INVALID_INPUT, refused.message, refused.details)
    if isinstance(exc, InvalidResourceInput):
        # The pydantic error list describing the request sent, beside what the kind takes.
        return Refusal(Code.INVALID_INPUT, exc.reason, exc.details)
    if isinstance(exc, HostRefusal):
        # Host-written text naming no request value (record_errors.py), and
        # what gets past it.
        return Refusal(exc.code, exc.reason, exc.details)
    if isinstance(exc, InsufficientScope):
        return Refusal(exc.code, exc.reason, {"scope": exc.scope})
    if isinstance(exc, AuthorizationError):
        # What tells a caller what it has to change.
        return Refusal(exc.code, exc.reason)
    if isinstance(exc, (QueryRefusal, ResourceError)):
        # A narrowing or a resource HQ cannot read, said in HQ's own sentence.
        return Refusal(Code.INVALID_INPUT, exc.reason)
    subject = name or "This request"
    if isinstance(exc, UpstreamUnavailable):
        # Every reader of a service outside HQ raises this for one that did
        # not answer (``http_read.UNREACHABLE``), whichever call reached it.
        logger.warning(
            "%s could not reach a service outside HQ: %s",
            _loggable(subject),
            _loggable(exc),
            extra={"event": "capability.upstream_unavailable"},
        )
        return Refusal(Code.UPSTREAM_UNAVAILABLE, f"{subject} could not reach a service outside HQ. Try again later.")
    # Neither handler nor dependency exception text crosses an adapter. It can
    # contain argument names, provider responses, paths, or values from the
    # request, so it stays in the log. The name is registry-owned and safe.
    if isinstance(exc, ValueError):
        # How a domain refuses a command it cannot act on, short of a HostRefusal.
        logger.warning(
            "%s was rejected: %s", _loggable(subject), _loggable(exc), extra={"event": "capability.rejected"}
        )
        return Refusal(Code.REJECTED, f"{subject} could not be executed.")
    logger.error(
        "%s could not be executed: %s",
        _loggable(subject),
        _loggable(exc),
        exc_info=exc,
        extra={"event": "capability.failed"},
    )
    return Refusal(Code.OPERATION_FAILED, f"{subject} could not be executed.")
