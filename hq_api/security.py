"""Who a machine client is, proven by a token HQ did not issue.

Pocket ID mints; HQ only verifies. That asymmetry is the whole design. There is
no credential table here to leak, no minting UI to guard, and no second answer
to "who may do this" -- revoking a client is done in the identity provider that
already owns every other credential in the fleet.

The permission keys declared on the Pocket ID API resource are deliberately the
*same strings* as HQ's capability names. A mapping table between the two would
be a third place for the authorization model to live, and the first thing to go
stale the next time a plugin adds a capability.
"""

from __future__ import annotations

import logging

from functools import cache
from typing import Any

import jwt
from django.conf import settings
from jwt import PyJWKClient

from application.security import AuthorizationError, Principal

# Claims a permission grant might arrive in. Pocket ID calls them "permissions
# (scopes)" and clients request them with the `scope` parameter, so `scope` is
# the expected one; the alternates cost nothing and mean a provider-side
# rename does not present as "your token grants no HQ permissions".
GRANT_CLAIMS = ("scope", "scp", "permissions")

INTERFACE = "api"


logger = logging.getLogger("severino.api")


class ClientReason(Exception):
    """An error carrying a sentence written for the caller.

    ``str(exception)`` is not that sentence. It is whatever the exception
    happens to hold -- a path, a query, a driver's own words -- and returning it
    from an API is how internal detail escapes one accident at a time. Static
    analysis reads it as stack-trace exposure for exactly that reason, and is
    right to: the guarantee cannot be "we only raise these types here", because
    that is true right up until someone catches a broader one.

    So the message a client sees is an attribute, set deliberately at the raise
    site. An exception that does not carry one has nothing to say publicly.
    """

    code = "error"

    def __init__(self, reason: str = "", *args: object) -> None:
        super().__init__(reason, *args)
        self.reason = reason


class TokenError(ClientReason):
    """A presented token is absent, malformed, expired, or not addressed to us."""

    code = "invalid_token"


@cache
def _jwks() -> PyJWKClient:
    # Cached deliberately: fetching the key set per request would put the
    # identity provider in the latency path of every import, and signing keys
    # rotate on a scale of months, not requests.
    return PyJWKClient(settings.OIDC_OP_JWKS_ENDPOINT, cache_keys=True)


def is_configured() -> bool:
    """False disables the surface fail-closed rather than trusting a default.

    Without a resource to check `aud` against, any token Pocket ID ever issued
    for any audience would verify here. An unset value must therefore mean
    "off", never "accept anything".
    """

    return bool(settings.SEVERINO_API_RESOURCE)


def verify(token: str) -> dict[str, Any]:
    """Return the claims of a valid access token, or raise.

    Signature alone is not enough and the check is not optional: a token minted
    for a *different* API resource on the same Pocket ID instance is correctly
    signed by the same key. `aud` is what stops it being accepted here.
    """

    if not is_configured():
        raise TokenError("The machine API is not configured.")
    try:
        signing_key = _jwks().get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=[settings.OIDC_RP_SIGN_ALGO],
            audience=settings.SEVERINO_API_RESOURCE,
            issuer=settings.OIDC_ISSUER,
            leeway=settings.SEVERINO_API_LEEWAY_SECONDS,
            # Named rather than assumed. A token without `exp` never expires,
            # and PyJWT will happily accept one if nothing insists otherwise.
            options={"require": ["exp", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        # The library's own message does not cross the boundary. It names the
        # signing key it looked for, the algorithms it would have accepted and
        # the audience it expected, which tells someone holding a rejected
        # token exactly what to change about the next one. The client is told
        # that the token was not accepted; the reason is logged here, where it
        # is useful and unreachable.
        logger.warning(
            "Rejected a machine API token: %s", exc, extra={"event": "api.token.rejected"}
        )
        raise TokenError("The access token was not accepted.") from exc


def granted(claims: dict[str, Any]) -> frozenset[str]:
    """The permissions the token actually carries, in HQ's own vocabulary."""

    for name in GRANT_CLAIMS:
        raw = claims.get(name)
        if raw is None:
            continue
        if isinstance(raw, str):
            return frozenset(raw.split())
        if isinstance(raw, (list, tuple)):
            return frozenset(str(item) for item in raw)
    return frozenset()


def api_principal(claims: dict[str, Any]) -> Principal:
    """A machine client acting with exactly what its token was granted.

    Note what this does *not* do: widen. A web operator holds every capability
    HQ has, and it would have been one line to hand a verified client the same
    set. The point of routing a Shortcut through an OAuth resource server is
    that a credential can run one narrow automation and nothing else --
    granting more here would throw that away and leave only the ceremony.
    """

    permissions = granted(claims)
    if not permissions:
        raise AuthorizationError(
            "This token grants no HQ permissions. Add them to the client's "
            "scope on the Pocket ID API resource."
        )
    # client_id for a client-credentials grant, sub for a user-delegated one.
    # Whichever it is lands in the audit log as the actor, so an import can
    # always be traced back to the credential that caused it.
    actor = (
        claims.get("client_id")
        or claims.get("azp")
        or claims.get("sub")
        or "unknown-client"
    )
    return Principal(str(actor), INTERFACE, frozenset(permissions))
