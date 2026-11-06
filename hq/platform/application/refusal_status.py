"""The one refusal contract: what every code HQ answers with means, and how a failure becomes one.

HQ answers a failed call with its own error code, and each code has one HTTP
status wherever it is answered. Mapping the ones that mean something other
than "you sent nonsense" keeps a client on HTTP status alone for control flow,
which is all a Shortcut can branch on comfortably, and keeps a refusal from
being logged as something it is not.

``refusal_of`` is the one place an exception becomes a refusal, so it is also
the one place an authorization refusal is recorded in the audit log.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, NotRequired, TypedDict, TypeIs

from django.core.exceptions import ValidationError as DjangoValidationError
from pydantic import Field, ValidationError as PydanticValidationError

from .answer_fields import Outcome, Pinned
from .input_errors import django_refusal, pydantic_refusal
from .projection import QueryRefusal
from .record_errors import HostRefusal
from .security import AuthorizationError, InsufficientScope, Principal
lazy from .approvals import RefusedHold
lazy from .denials import record_denial
lazy from .integration_specs import CapabilitySpec, takes_stale_write_token
lazy from .resources import InvalidResourceInput, ResourceError

logger = logging.getLogger("severino.capabilities")


class Door(StrEnum):
    """Where a code can be answered, so each operation lists only the codes that can reach it."""

    # Any door admitting its caller, before anything runs.
    ADMISSION = "admission"
    # Any API endpoint: a method or surface it does not serve, a grant its view
    # checks, or a failure HQ did not expect.
    ENDPOINT = "endpoint"
    # A request body that never reaches a capability.
    BODY = "body"
    # A door that stops waiting for a call: MCP's.
    WAIT = "wait"
    # A resource read.
    RESOURCE = "resource"
    # Every capability call: its authority, its policy, its command's shape,
    # and whatever its handler raises, which no capability declares.
    CAPABILITY = "capability"
    # A capability call by a name nothing registered.
    NAME = "name"
    # A capability that acts on a target, called without one.
    TARGETED = "targeted"
    # A capability that takes no target, called with one.
    UNTARGETED = "untargeted"
    # A handler that checks the caller's stale-write token.
    STALE_WRITE = "stale_write"
    # A change's retry key.
    RETRY_KEY = "retry_key"
    # A change a person must agree to, which could not be held.
    HOLD = "hold"
    # A held change, once a person declined it or it lapsed.
    DECISION = "decision"


@dataclass(frozen=True, slots=True)
class Code:
    """One code's facts: its status, the doors that answer it, and whether it says "later"."""

    status: int
    doors: frozenset[Door]
    # Only a refusal that says "later" carries ``retry_after``, and the API's
    # ``Retry-After`` header repeats it.
    waits: bool = False


def _code(status: int, *doors: Door, waits: bool = False) -> Code:
    return Code(status, frozenset(doors), waits)


# Every code, once, with its facts. Each set below is derived from it.
CODES = {
    "unknown_capability": _code(404, Door.NAME),
    "forbidden": _code(403, Door.ENDPOINT, Door.CAPABILITY),
    # The operator's standing rule refuses this client. Like a missing grant,
    # no change to the request gets it through.
    "denied_by_policy": _code(403, Door.CAPABILITY),
    "not_found": _code(404, Door.RESOURCE, Door.CAPABILITY),
    "unknown_resource": _code(404, Door.RESOURCE),
    # The resource exists, and has no list or detail to read.
    "unsupported_operation": _code(405, Door.RESOURCE),
    # The call had to be held, and the record it names could not be read to
    # show the person deciding: the target is not there.
    "approval_subject_unreadable": _code(404, Door.HOLD),
    # A held call that could not be made to wait; its kinds say why.
    "refused_hold": _code(409, Door.HOLD),
    # A held call a person refused: asking again the same way gets the same answer.
    "declined": _code(403, Door.DECISION),
    # A held call nobody decided in time, or whose subject moved before it ran.
    "expired": _code(409, Door.DECISION),
    "stale": _code(409, Door.DECISION),
    "conflict": _code(409, Door.CAPABILITY, Door.STALE_WRITE),
    "idempotency_conflict": _code(409, Door.RETRY_KEY),
    # The request is valid; the state it acts on refuses it until it changes.
    "precondition": _code(409, Door.CAPABILITY),
    # The domain refused the command as sent (a confirmation that does not match).
    "rejected": _code(409, Door.CAPABILITY),
    # The work ran to its end and failed; the message is the job's own note.
    "work_failed": _code(409, Door.CAPABILITY),
    # Something HQ did not expect went wrong: the log holds it, the answer says nothing of it.
    "operation_failed": _code(500, Door.ENDPOINT, Door.CAPABILITY),
    "invalid_input": _code(400, Door.RESOURCE, Door.CAPABILITY),
    "target_required": _code(400, Door.TARGETED),
    "target_not_allowed": _code(400, Door.UNTARGETED),
    # The uploaded certificate or key cannot be used; the message says why.
    "invalid_material": _code(400, Door.CAPABILITY),
    # Nothing is wrong with the request. This client is holding more decisions
    # than a person has answered, so the honest status is the one that says
    # "later", not the one that says "malformed".
    "too_many_pending_approvals": _code(429, Door.HOLD),
    # The agent is calling faster than its rate, or has as many calls in
    # flight as it, or every agent together, may have: later, never malformed.
    "rate_limited": _code(429, Door.ADMISSION, waits=True),
    "too_many_calls": _code(429, Door.ADMISSION, waits=True),
    # The token is missing, malformed, expired or not HQ's.
    "invalid_token": _code(401, Door.ADMISSION),
    # A good token that grants nothing HQ can use, or lacks a permission its
    # person holds: granting it to the client and signing in again gets it
    # through, and `details.scope` names it.
    "insufficient_scope": _code(403, Door.ADMISSION, Door.ENDPOINT, Door.CAPABILITY),
    # The operator's brakes: no change to the request gets it through.
    "agents_paused": _code(403, Door.ADMISSION),
    "agent_blocked": _code(403, Door.ADMISSION),
    # A tailnet device nobody approved for this agent, or one a person blocked:
    # approving or allowing it on the Agents page gets it through.
    "device_awaiting_approval": _code(403, Door.ADMISSION),
    "device_blocked": _code(403, Door.ADMISSION),
    # HQ stopped waiting for a call that may still finish.
    "call_timed_out": _code(504, Door.WAIT),
    "not_configured": _code(503, Door.ENDPOINT),
    "method_not_allowed": _code(405, Door.ENDPOINT),
    "unsupported_media_type": _code(415, Door.BODY),
    "request_too_large": _code(413, Door.BODY),
    "invalid_json": _code(400, Door.BODY),
    "idempotency_key_required": _code(400, Door.RETRY_KEY),
    "invalid_idempotency_key": _code(400, Door.RETRY_KEY),
}


def statuses(table: Mapping[str, Code] = CODES) -> dict[str, int]:
    """Each code's one status, wherever it is answered."""

    return {code: facts.status for code, facts in table.items()}


def answered_by(*doors: Door, table: Mapping[str, Code] = CODES) -> frozenset[str]:
    """The codes any of ``doors`` can answer."""

    return frozenset(code for code, facts in table.items() if facts.doors & set(doors))


def waiting(table: Mapping[str, Code] = CODES) -> frozenset[str]:
    """The codes that say "later"."""

    return frozenset(code for code, facts in table.items() if facts.waits)


REFUSAL_STATUS = statuses()
LATER_CODES = waiting()
# The codes a missing grant answers: the caller's own, or its token's.
WANTS_A_GRANT = frozenset({"forbidden", "insufficient_scope"})

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
        changes=spec.effect != "read",
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
type RefusalDetails = Annotated[
    Any,
    Field(
        description=(
            "What gets past the refusal, where something does: `next`, the call that finds what was "
            "missing; `closest`, the read of each record or list named nearest the one asked for; "
            "`field`, the argument to send; `scope`, the permission to grant the token's client; for "
            "`invalid_input`, each field that was wrong and why, which a list or record read puts under "
            "`errors`, beside the `filters` its list takes."
        )
    ),
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

    code: str
    message: str
    details: Any = None
    # Seconds until asking again can succeed, for a refusal that says "later".
    retry_after: int | None = None

    def __post_init__(self) -> None:
        # A code the table does not hold is a defect in whoever answered it, and
        # a client can key on no code it was never told of, so it is answered as
        # the failure it is. The code stays in the log.
        if self.code not in REFUSAL_STATUS:
            logger.error("Refusal code %s has no status.", _loggable(self.code), extra={"event": "refusal.unlisted"})
            for name, value in (
                ("code", "operation_failed"),
                ("message", "This request could not be executed."),
                ("details", None),
                ("retry_after", None),
            ):
                object.__setattr__(self, name, value)
        # The API document declares the wait only where a code says "later".
        if self.retry_after is not None and self.code not in LATER_CODES:
            logger.error(
                "Refusal code %s carried a wait.", _loggable(self.code), extra={"event": "refusal.unlisted_wait"}
            )
            object.__setattr__(self, "retry_after", None)

    @property
    def status(self) -> int:
        return REFUSAL_STATUS[self.code]

    @property
    def scope(self) -> str:
        """The permission an ``insufficient_scope`` refusal names, which a door's challenge repeats; "" for any other."""

        if self.code != "insufficient_scope" or not isinstance(self.details, Mapping):
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


def refuse(code: str, message: str, details: Any = None) -> dict[str, Any]:
    """A refusal's envelope, built where the refusal is decided."""

    return Refusal(code, message, details).answer()


def refused(answer: Mapping[str, object]) -> TypeIs[RefusalEnvelope]:
    """Whether a capability's answer is its refusal, so the rest of it reads as the success it is."""

    return answer.get("ok") is False


def next_call(tool: str, **arguments: Any) -> dict[str, Any]:
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
        return Refusal(
            error.get("code", "operation_failed"),
            error.get("message", f"{name or 'This request'} could not be executed."),
            error.get("details"),
            error.get("retry_after"),
        )
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
        return Refusal("invalid_input", refused.message, refused.details)
    if isinstance(exc, DjangoValidationError):
        refused = django_refusal(name, exc)
        return Refusal("invalid_input", refused.message, refused.details)
    if isinstance(exc, InvalidResourceInput):
        # The pydantic error list describing the request sent, beside what the kind takes.
        return Refusal("invalid_input", exc.reason, exc.details)
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
        return Refusal("invalid_input", exc.reason)
    subject = name or "This request"
    # Neither handler nor dependency exception text crosses an adapter. It can
    # contain argument names, provider responses, paths, or values from the
    # request, so it stays in the log. The name is registry-owned and safe.
    if isinstance(exc, ValueError):
        # How a domain refuses a command it cannot act on, short of a HostRefusal.
        logger.warning(
            "%s was rejected: %s", _loggable(subject), _loggable(exc), extra={"event": "capability.rejected"}
        )
        return Refusal("rejected", f"{subject} could not be executed.")
    logger.error(
        "%s could not be executed: %s",
        _loggable(subject),
        _loggable(exc),
        exc_info=exc,
        extra={"event": "capability.failed"},
    )
    return Refusal("operation_failed", f"{subject} could not be executed.")
