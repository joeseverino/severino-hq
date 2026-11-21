"""Every code HQ refuses with, declared once with its facts; every set of codes is derived from it.

A module of its own, importing nothing of HQ's, so each refusal class names
its member as it is declared.
"""

from enum import StrEnum, auto
from typing import Self


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
    # A held change, once an operator rejected it or it lapsed.
    DECISION = "decision"


# Marks a code that says "later": only its refusal carries ``retry_after``,
# and the API's ``Retry-After`` header repeats it.
LATER = True


class Code(StrEnum):
    """One refusal code: its one status wherever it is answered, why it refused, the doors that answer it,
    and whether it says "later"."""

    status: int
    # Why, as the end of the audit log's sentence for the refusal: "example was
    # refused “Edit a record”: it has no grant for this".
    said: str
    doors: frozenset[Door]
    waits: bool

    def __new__(cls, value: str, status: int, said: str, doors: tuple[Door, ...], waits: bool = False) -> Self:
        code = str.__new__(cls, value)
        code._value_ = value
        code.status, code.said, code.doors, code.waits = status, said, frozenset(doors), waits
        return code

    UNKNOWN_CAPABILITY = auto(), 404, "HQ has no command by that name", (Door.NAME,)
    FORBIDDEN = auto(), 403, "it has no grant for this", (Door.ENDPOINT, Door.CAPABILITY)
    # The operator's standing rule refuses this client. Like a missing grant,
    # no change to the request gets it through.
    DENIED_BY_POLICY = auto(), 403, "the operator's rule refuses this client", (Door.CAPABILITY,)
    # Also a field's ``ValidationError`` code where the field names a record (a
    # model's reference column), so the call is answered as a missing record is.
    NOT_FOUND = auto(), 404, "the record it names is not there", (Door.RESOURCE, Door.CAPABILITY)
    UNKNOWN_RESOURCE = auto(), 404, "HQ has no list by that name", (Door.RESOURCE,)
    # The resource exists, and has no list or detail to read.
    UNSUPPORTED_OPERATION = auto(), 405, "that list has nothing of that kind to read", (Door.RESOURCE,)
    # The call had to be held, and the record it names could not be read to
    # show the person deciding: the target is not there.
    APPROVAL_SUBJECT_UNREADABLE = (
        auto(),
        404,
        "the record it names could not be read to show the person deciding",
        (Door.HOLD,),
    )
    # A held call that could not be made to wait; its kinds say why.
    REFUSED_HOLD = auto(), 409, "it could not be held for a person to decide", (Door.HOLD,)
    # A held call an operator rejected: asking again the same way gets the same answer.
    OPERATOR_REJECTED = auto(), 403, "an operator rejected it", (Door.DECISION,)
    # A held call nobody decided in time, or whose subject moved before it ran.
    EXPIRED = auto(), 409, "nobody decided it in time", (Door.DECISION,)
    STALE = auto(), 409, "what it acts on changed before it ran", (Door.DECISION,)
    # A held call its asker took back before anybody decided it.
    WITHDRAWN = auto(), 409, "its asker withdrew it", (Door.DECISION,)
    CONFLICT = auto(), 409, "what it acts on changed since it was read", (Door.CAPABILITY, Door.STALE_WRITE)
    IDEMPOTENCY_CONFLICT = auto(), 409, "its retry key was sent before with a different request", (Door.RETRY_KEY,)
    # The request is valid; the state it acts on refuses it until it changes.
    PRECONDITION = auto(), 409, "what it acts on does not allow it yet", (Door.CAPABILITY,)
    # The domain refused the command as sent (a confirmation that does not match).
    REJECTED = auto(), 409, "HQ refused the command as sent", (Door.CAPABILITY,)
    # The work ran to its end and failed; the message is the job's own note.
    WORK_FAILED = auto(), 409, "the work ran and failed", (Door.CAPABILITY,)
    # Something HQ did not expect went wrong: the log holds it, the answer says nothing of it.
    OPERATION_FAILED = auto(), 500, "something HQ did not expect went wrong", (Door.ENDPOINT, Door.CAPABILITY)
    # A service outside HQ that the call reaches did not answer, or answered
    # nothing usable: the request is sound, and asking again may succeed.
    UPSTREAM_UNAVAILABLE = (
        auto(),
        503,
        "a service outside HQ that it reaches did not answer",
        (Door.ENDPOINT, Door.CAPABILITY),
    )
    INVALID_INPUT = auto(), 400, "what it sent is not valid", (Door.RESOURCE, Door.CAPABILITY)
    TARGET_REQUIRED = auto(), 400, "it names no record to act on", (Door.TARGETED,)
    TARGET_NOT_ALLOWED = auto(), 400, "it names a record, and this acts on none", (Door.UNTARGETED,)
    # The uploaded certificate or key cannot be used; the message says why.
    INVALID_MATERIAL = auto(), 400, "the certificate or key cannot be used", (Door.CAPABILITY,)
    # Nothing is wrong with the request. This client is holding more decisions
    # than a person has answered, so the honest status is the one that says
    # "later", not the one that says "malformed".
    TOO_MANY_PENDING_APPROVALS = auto(), 429, "it is holding more requests than a person has answered", (Door.HOLD,)
    # The agent is calling faster than its rate, or has as many calls in
    # flight as it, or every agent together, may have: later, never malformed.
    RATE_LIMITED = auto(), 429, "it is calling faster than its rate", (Door.ADMISSION,), LATER
    TOO_MANY_CALLS = auto(), 429, "it has as many calls in flight as it may", (Door.ADMISSION,), LATER
    # The token is missing, malformed, expired or not HQ's.
    INVALID_TOKEN = auto(), 401, "its access token is missing, malformed, expired or not HQ's", (Door.ADMISSION,)
    # A good token that grants nothing HQ can use, or lacks a permission its
    # person holds: granting it to the client and signing in again gets it
    # through, and `details.scope` names it.
    INSUFFICIENT_SCOPE = (
        auto(),
        403,
        "its token lacks a permission this needs",
        (Door.ADMISSION, Door.ENDPOINT, Door.CAPABILITY),
    )
    # The operator's brakes: no change to the request gets it through.
    AGENTS_PAUSED = auto(), 403, "agents are paused", (Door.ADMISSION,)
    AGENT_BLOCKED = auto(), 403, "the operator blocked this client", (Door.ADMISSION,)
    # A tailnet device nobody approved for this agent, or one a person blocked:
    # approving or allowing it on the Agents page gets it through.
    DEVICE_AWAITING_APPROVAL = auto(), 403, "nobody has approved it on this device yet", (Door.ADMISSION,)
    DEVICE_BLOCKED = auto(), 403, "it is blocked on this device", (Door.ADMISSION,)
    # The tailnet could not place the call: no device sent it, so nothing can be
    # approved, or tailscaled did not answer, so asking again may.
    DEVICE_UNKNOWN = auto(), 403, "no tailnet device sent the call", (Door.ADMISSION,)
    TAILNET_UNAVAILABLE = auto(), 503, "HQ could not ask tailscaled which device sent the call", (Door.ADMISSION,)
    # HQ stopped waiting for a call that may still finish.
    CALL_TIMED_OUT = auto(), 504, "HQ stopped waiting for the call", (Door.WAIT,)
    NOT_CONFIGURED = auto(), 503, "HQ is not set up for this", (Door.ENDPOINT,)
    METHOD_NOT_ALLOWED = auto(), 405, "that address does not take this method", (Door.ENDPOINT,)
    UNSUPPORTED_MEDIA_TYPE = auto(), 415, "its body is not a type HQ takes", (Door.BODY,)
    REQUEST_TOO_LARGE = auto(), 413, "its body is too large", (Door.BODY,)
    INVALID_JSON = auto(), 400, "its body is not valid JSON", (Door.BODY,)
    IDEMPOTENCY_KEY_REQUIRED = auto(), 400, "it sent no retry key", (Door.RETRY_KEY,)
    INVALID_IDEMPOTENCY_KEY = auto(), 400, "its retry key is not valid", (Door.RETRY_KEY,)


def answered_by(*doors: Door) -> frozenset[Code]:
    """The codes any of ``doors`` can answer."""

    return frozenset(code for code in Code if code.doors & set(doors))


LATER_CODES = frozenset(code for code in Code if code.waits)
