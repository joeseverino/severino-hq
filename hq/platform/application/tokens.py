"""Who a token HQ did not issue names: an agent's or a device's, and a browser's sign-in.

Pocket ID mints and HQ only verifies, so there is no credential here to leak
and revoking a client happens where every other credential is revoked. Its
permission keys are HQ's capability names, with no mapping table between them
(HQ verifies; it does not issue, in API). Every door a token reaches (the
machine API, MCP, the browser's sign-in and sign-out) reads it here, under
the application, so no door depends on another.
"""

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import jwt
from django.conf import settings
from jwt import PyJWK, PyJWKClient

from . import own_devices
from .linked_accounts import sign_in_subject
from .own_devices import REGISTERED_CLIENT_ID
from .security import (
    AuthorizationError,
    Principal,
    agent_principal,
    device_principal,
    is_agent,
)

# The claims Pocket ID writes a token's grant in: `scope` as RFC 9068 names
# it, and `scp` beside it (`JWTScopeFieldBoth`).
GRANT_CLAIMS = ("scope", "scp")

# The permission on HQ's Pocket ID API resource that marks a client as a
# person's own device (a phone app, a Shortcut), which an administrator gives
# per client. It is a standing, never a capability, so it grants nothing on
# its own (docs/Security.md#agents).
DEVICE_SCOPE = "own_device"

# The scopes a sign-in asks Pocket ID for about the person and the session
# (its `standardScopes`), which a delegated token carries beside HQ's own
# permissions. They say who signed in, never what HQ allows, so none of them
# is ever a permission here.
IDENTITY_SCOPES = frozenset({"openid", "profile", "email", "groups", "offline_access"})

# The header `typ` of each kind of token HQ verifies, compared without case,
# so a token the provider signs for one use is refused at the other. An RFC
# 9068 access token names its media type or the short form; Pocket ID's ID
# token carries fosite's default, `JWT`.
ACCESS_TOKEN_TYPES = frozenset({"at+jwt", "application/at+jwt"})
ID_TOKEN_TYPES = frozenset({"jwt"})


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
    """Keep ``KEYS`` current for as long as the web application runs.

    Kept whenever there is an issuer: a browser's sign-in reads them as well
    as the machine API.
    """

    if not settings.OIDC_ISSUER:
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
    return decode(
        token,
        signing_key(token),
        audience=settings.SEVERINO_API_RESOURCE,
        types=ACCESS_TOKEN_TYPES,
        leeway=settings.SEVERINO_API_LEEWAY_SECONDS,
    )


def signing_key(token: str) -> PyJWK:
    """The held key ``token``'s header names, or ``TokenError``: never a read of the key set."""

    return KEYS.find(str(_header(token).get("kid") or ""))


def decode(token: str, key: PyJWK, *, audience: str, types: frozenset[str], leeway: float = 0) -> dict[str, Any]:
    """The claims of a token the issuer signed with ``key`` for ``audience``, of a type in ``types``.

    The one check of every token HQ is handed, a browser's ID token and an
    agent's access token alike, so each is held to the same algorithm, issuer
    and required claims. An empty audience or issuer refuses every token.
    """

    issuer = settings.OIDC_ISSUER
    if not audience or not issuer:
        raise TokenError("The access token was not accepted.")
    try:
        # Anything else the provider signs (an ID token, a logout token) is
        # not the token asked for, whatever audience it names (RFC 9068
        # section 4, RFC 8725 section 3.11).
        kind = str(_header(token).get("typ") or "").lower()
        if kind not in types:
            raise jwt.InvalidTokenError(f"its type is {kind!r}")
        return jwt.decode(
            token,
            key,
            algorithms=[settings.OIDC_RP_SIGN_ALGO],
            audience=audience,
            issuer=issuer,
            leeway=leeway,
            # Named rather than assumed. A token without `exp` never expires,
            # and PyJWT will happily accept one if nothing insists otherwise.
            options={"require": ["exp", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise _refused(exc) from exc


def _header(token: str) -> dict[str, Any]:
    try:
        return jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise _refused(exc) from exc


def _refused(exc: jwt.PyJWTError) -> TokenError:
    # The library's own message does not cross the boundary. It names the
    # signing key it looked for, the algorithms it would have accepted and
    # the audience it expected, which tells someone holding a rejected token
    # exactly what to change about the next one. The caller is told that the
    # token was not accepted; the reason is logged here, where it is useful
    # and unreachable.
    logger.warning("Rejected a token: %s", exc, extra={"event": "api.token.rejected"})
    return TokenError("The access token was not accepted.")


def granted(claims: dict[str, Any]) -> frozenset[str]:
    """The permissions the token actually carries, in HQ's own vocabulary: never an identity scope."""

    for name in GRANT_CLAIMS:
        raw = claims.get(name)
        if raw is None:
            continue
        if isinstance(raw, str):
            return frozenset(raw.split()) - IDENTITY_SCOPES
        if isinstance(raw, (list, tuple)):
            return frozenset(str(item) for item in raw) - IDENTITY_SCOPES
    return frozenset()


def token_principal(claims: dict[str, Any], interface: str) -> Principal:
    """The agent or the person's device a verified token names, on the surface it called through.

    Every surface a token reaches turns it into a principal here. A token
    carrying ``DEVICE_SCOPE`` from a client its person registered in HQ as
    their own is ``device_principal``'s: its grant within its person's role,
    never paused. Every other is ``agent_principal``'s: its grant, never
    widened, within the role of the person who signed it in, if one did. A token naming a person with no role
    is refused.
    """

    grant = granted(claims)
    device = DEVICE_SCOPE in grant
    permissions = grant - {DEVICE_SCOPE}
    if not permissions:
        raise AuthorizationError(
            "This token grants no HQ permissions. Add them to the client's scope on the Pocket ID API resource."
        )
    # The client that presented the token, which Pocket ID's RFC 9068 access
    # tokens always name in client_id, on a delegated grant too (there `sub` is
    # the person who allowed the client). It lands in the audit log as the
    # actor, so an import can always be traced back to the credential that
    # caused it; `azp` and `sub` are read only from an issuer that omits it.
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
    actor = str(actor).strip()
    person_key = _person_key(claims, actor)
    if not device:
        return agent_principal(actor, interface, permissions, person_key=person_key)
    if not is_agent(interface):
        raise ValueError(f"{interface!r} is not an interface a token reaches HQ through.")
    _require_a_persons_client(claims, person_key)
    # The provider's grant is one half; the other is the person's own word in
    # HQ, so a grant given to an agent's client by mistake promotes nothing.
    # Unregistered, it is an agent, its grant still naming the permission so
    # the Agents page can say what the provider gave it.
    if not own_devices.registered(actor, person_key):
        return agent_principal(actor, interface, grant, person_key=person_key)
    return device_principal(actor, permissions, grant, person_key=person_key)


def _person_key(claims: dict[str, Any], actor: str) -> str:
    """The sign-in key of the person who signed this token in, or "" for a machine's own token.

    Pocket ID names a client-credentials token's subject after the client
    itself (``client-<client_id>``), and an issuer that names no client
    leaves its subject as the actor, so neither has a person behind it.
    """

    subject = str(claims.get("sub") or "").strip()
    client = str(claims.get("client_id") or actor)
    if not subject or subject in {actor, f"client-{client}"}:
        return ""
    return sign_in_subject(settings.OIDC_ISSUER, subject)


def _require_a_persons_client(claims: dict[str, Any], person_key: str) -> None:
    """Refuse a device standing whose token is not a registered client acting for a person.

    The scope is given per registered client, on a grant a person allowed; a
    token that carries it otherwise came through a grant meant for many
    clients or for none, which is a misconfiguration to fix, never a device.
    """

    client = str(claims.get("client_id") or "")
    if not REGISTERED_CLIENT_ID.fullmatch(client):
        raise AuthorizationError(
            f"This token carries {DEVICE_SCOPE!r} but names no registered client in `client_id`. "
            "Give it only to a person's own device clients on the Pocket ID API resource."
        )
    if not person_key:
        raise AuthorizationError(
            f"This token carries {DEVICE_SCOPE!r} without a person: a device's token is one a person signed in for."
        )
