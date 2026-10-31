"""The HTTP status a capability's refusal answers with, in every adapter that speaks HTTP.

HQ answers a failed capability with its own error code. Mapping the ones that
mean something other than "you sent nonsense" keeps a client on HTTP status
alone for control flow, which is all a Shortcut can branch on comfortably, and
keeps a refusal from being logged as something it is not.
"""

CAPABILITY_STATUS = {
    "unknown_capability": 404,
    "forbidden": 403,
    # The operator's standing rule refuses this client. Like a missing grant,
    # no change to the request gets it through.
    "denied_by_policy": 403,
    "not_found": 404,
    # The call had to be held, and the record it names could not be read to
    # show the person deciding: the target is not there.
    "approval_subject_unreadable": 404,
    "conflict": 409,
    "operation_failed": 409,
    "idempotency_conflict": 409,
    # The request is valid; the state it acts on refuses it until it changes.
    "precondition": 409,
    # The uploaded certificate or key cannot be used; the message says why.
    "invalid_material": 400,
    # Nothing is wrong with the request. This client is holding more decisions
    # than a person has answered, so the honest status is the one that says
    # "later", not the one that says "malformed".
    "too_many_pending_approvals": 429,
}


def refusal_status(code: str) -> int:
    """The status for a refusal's ``code``; any other is the request's to change."""

    return CAPABILITY_STATUS.get(code, 400)
