"""Who a machine client is, proven by a token HQ did not issue.

Pocket ID mints and HQ only verifies, so there is no credential here to leak
and revoking a client happens where every other credential is revoked. Its
permission keys are HQ's capability names, with no mapping table between them
(HQ verifies; it does not issue, in API).
"""

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import jwt
from django.conf import settings
from jwt import PyJWK, PyJWKClient

from hq.platform.application.security import AuthorizationError, Principal

# Claims a permission grant might arrive in. Pocket ID calls them "permissions
# (scopes)" and clients request them with the `scope` parameter, so `scope` is
# the expected one; the alternates cost nothing and mean a provider-side
# rename does not present as "your token grants no HQ permissions".
GRANT_CLAIMS = ("scope", "scp", "permissions")

INTERFACE = "api"


logger = logging.getLogger("severino.api")


class ClientReason(Exception):
    """An error carrying a sentence written for the caller.

    ``str(exception)`` holds whatever the error happened to (a path, a query, a
    driver's own words), and which types are raised here can't keep it out of
    a response, since one broader catch anywhere ends that. So the sentence a
    client sees is ``reason``, set at the raise site, and an error without one
    has nothing to say publicly.
    """

    code = "error"

    def __init__(self, reason: str = "", *args: object) -> None:
        super().__init__(reason, *args)
        self.reason = reason


class TokenError(ClientReason):
    """A presented token is absent, malformed, expired, or not addressed to HQ."""

    code = "invalid_token"


# Seconds between reads of the identity provider's key set, and the least
# time between two reads, which a token naming an unknown key may ask for early.
REFRESH_EVERY = 3600.0
REFRESH_AT_MOST_EVERY = 60.0
# How long stopping waits for a read in flight, in seconds.
STOPS_WITHIN = 5.0


class SigningKeys:
    """The identity provider's signing keys, held in the process by key id.

    A request never fetches them (``hq.platform.core.outbound``): they are read
    when the application starts and again every ``REFRESH_EVERY`` on a thread
    of their own (``keeping_keys``), and a token naming a key not held asks
    that thread to read again instead of waiting for it.
    """

    def __init__(self) -> None:
        self.held: dict[str, PyJWK] = {}
        self.wanted = threading.Event()

    def load(self) -> None:
        """Read the key set now. Never while serving a request."""

        client = PyJWKClient(settings.OIDC_OP_JWKS_ENDPOINT, cache_jwk_set=False, timeout=settings.OIDC_TIMEOUT)
        self.held = {key.key_id: key for key in client.get_signing_keys() if key.key_id}

    def find(self, kid: str) -> PyJWK:
        """The held key ``kid`` names, or ``TokenError`` and a read asked for."""

        key = self.held.get(kid)
        if key is not None:
            return key
        self.wanted.set()
        if self.held:
            logger.warning(
                "No loaded signing key has the token's key id; the key set will be read again.",
                extra={"event": "api.keys.unknown"},
            )
        else:
            logger.warning(
                "The identity provider's signing keys are not loaded, so no token can be verified yet.",
                extra={"event": "api.keys.not_loaded"},
            )
        raise TokenError("The access token was not accepted.")


KEYS = SigningKeys()


@contextmanager
def keeping_keys() -> Iterator[None]:
    """Keep ``KEYS`` current for as long as the web application runs."""

    if not is_configured():
        yield
        return
    stopping = threading.Event()

    def run() -> None:
        while True:
            try:
                KEYS.load()
            except Exception:  # the keys held stay; the next read tries again
                logger.exception("api.keys.load_failed")
            KEYS.wanted.clear()
            if stopping.wait(REFRESH_AT_MOST_EVERY):
                return
            KEYS.wanted.wait(REFRESH_EVERY - REFRESH_AT_MOST_EVERY)
            if stopping.is_set():
                return

    thread = threading.Thread(target=run, name="hq-signing-keys", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopping.set()
        KEYS.wanted.set()
        thread.join(STOPS_WITHIN)


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
        signing_key = KEYS.find(str(jwt.get_unverified_header(token).get("kid") or ""))
        return jwt.decode(
            token,
            signing_key,
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
        logger.warning("Rejected a machine API token: %s", exc, extra={"event": "api.token.rejected"})
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
    HQ has, and handing a verified client the same set would be one line. The point of routing a Shortcut through an OAuth resource server is
    that a credential can run one narrow automation and nothing else,
    granting more here would throw that away and leave only the ceremony.
    """

    permissions = granted(claims)
    if not permissions:
        raise AuthorizationError(
            "This token grants no HQ permissions. Add them to the client's scope on the Pocket ID API resource."
        )
    # client_id for a client-credentials grant, sub for a user-delegated one.
    # Whichever it is lands in the audit log as the actor, so an import can
    # always be traced back to the credential that caused it.
    #
    # Refused rather than defaulted: a shared placeholder actor would put two
    # unrelated credentials in one idempotency namespace. A verified action nobody can be attributed to
    # is worse than a rejected one.
    actor = claims.get("client_id") or claims.get("azp") or claims.get("sub")
    if not str(actor or "").strip():
        raise AuthorizationError(
            "This token names no client. Its `client_id`, `azp` or `sub` claim "
            "is what HQ records as the actor for everything it does."
        )
    return Principal(
        str(actor).strip(),
        INTERFACE,
        frozenset(permissions),
        granted=frozenset(permissions),
    )
