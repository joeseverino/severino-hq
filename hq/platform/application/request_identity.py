"""Who the session says the reader is, and which provider vouched for it."""

from dataclasses import dataclass
from datetime import datetime

from django.conf import settings

from . import tailnet


@dataclass(frozen=True, slots=True)
class Identity:
    """Who is asking, according to each system that independently knows."""

    username: str = ""
    email: str = ""
    full_name: str = ""
    sso_only: bool = False
    backends: tuple[str, ...] = ()
    session_expires: datetime | None = None
    tailnet_user: str = ""
    # Which backend actually signed this session in, rather than which ones are
    # installed. Django records it on the session at login, so it is the one
    # statement about this session that cannot be inferred from configuration.
    signed_in_by: str = ""
    provider: str = ""
    groups: tuple[str, ...] = ()
    staff: bool = False
    superuser: bool = False
    last_sign_in: datetime | None = None
    token_expires: str = ""
    provider_principal: str = ""

    @property
    def session_principals(self) -> frozenset[str]:
        return frozenset(
            principal.strip().casefold()
            for principal in (self.username, self.email)
            if principal.strip()
        )

    @property
    def route(self) -> str:
        """How this session was established, in a word."""

        if not self.signed_in_by:
            return "unknown"
        return "single sign-on" if "OIDC" in self.signed_in_by else "password"

    @property
    def corroborated(self) -> bool:
        """Whether two independent systems agree on who is asking.

        The session says who signed in; the tailnet says which account owns the
        device the request came from. Neither consults the other, so agreement
        between them is independent corroboration. Different account
        namespaces require an explicit link; HQ does not infer equivalence
        from similar-looking names.
        """

        tailnet = self.tailnet_user.strip().casefold()
        if not tailnet or not self.session_principals:
            return False
        if tailnet in self.session_principals:
            return True
        return tailnet == self.provider_principal.strip().casefold()

    @property
    def agreement_basis(self) -> str:
        tailnet = self.tailnet_user.strip().casefold()
        if tailnet in self.session_principals:
            return "exact principal"
        if tailnet == self.provider_principal.strip().casefold():
            return "SSO-signed principal link"
        return ""

    @property
    def conflicted(self) -> bool:
        """Whether both systems answered, but named different people."""

        tailnet = self.tailnet_user.strip().casefold()
        _, separator, namespace = tailnet.rpartition("@")
        return bool(
            separator
            and not self.corroborated
            and any(
                principal.rpartition("@")[2] == namespace
                for principal in self.session_principals
                if "@" in principal
            )
        )


def identity_of(request, device: tailnet.Device | None) -> Identity:
    from hq.platform.core.oidc import TAILSCALE_PRINCIPAL_SESSION_KEY

    user = getattr(request, "user", None)
    backends = tuple(getattr(settings, "AUTHENTICATION_BACKENDS", ()))
    session = getattr(request, "session", None)
    expiry = None
    if session is not None:
        try:
            expiry = session.get_expiry_date()
        except (AttributeError, ValueError, TypeError):
            expiry = None
    signed_in_by = str(
        (session or {}).get("_auth_user_backend", "") if session is not None else ""
    )
    return Identity(
        username=getattr(user, "username", "") or "",
        email=getattr(user, "email", "") or "",
        full_name=(getattr(user, "get_full_name", lambda: "")() or "").strip(),
        # Whether a password could sign anyone in at all. Read from what is
        # installed rather than from a flag saying it is not, because the
        # backend list is the thing Django actually consults.
        sso_only=bool(backends)
        and not any(name.endswith("ModelBackend") for name in backends),
        backends=backends,
        session_expires=expiry,
        tailnet_user=device.user if device else "",
        signed_in_by=signed_in_by,
        # Who vouched for the person, taken from the endpoint HQ actually sends
        # them to rather than from a name written down beside it. A configured
        # provider did not vouch for a password session, so it must not appear
        # beside that session as though it did.
        provider=_provider_host() if "OIDC" in signed_in_by else "",
        groups=tuple(
            getattr(user, "groups", None).values_list("name", flat=True)
            if getattr(user, "pk", None)
            else ()
        ),
        staff=bool(getattr(user, "is_staff", False)),
        superuser=bool(getattr(user, "is_superuser", False)),
        last_sign_in=getattr(user, "last_login", None),
        token_expires=str(
            (session or {}).get("oidc_id_token_expiration", "")
            if session is not None
            else ""
        ),
        provider_principal=(
            str((session or {}).get(TAILSCALE_PRINCIPAL_SESSION_KEY, ""))
            if session is not None and "OIDC" in signed_in_by
            else ""
        ),
    )


def _provider_host() -> str:
    """The identity provider, named by where sign-in is actually sent."""

    from urllib.parse import urlsplit

    endpoint = str(getattr(settings, "OIDC_OP_AUTHORIZATION_ENDPOINT", "") or "")
    return urlsplit(endpoint).hostname or ""
