"""Who the session says the reader is, and which provider vouched for it; for
an agent's token, which has no session, the device it called from and who approved it."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
lazy from urllib.parse import urlsplit

from django.conf import settings

lazy from hq.platform.core.oidc import TAILSCALE_PRINCIPAL_SESSION_KEY, sso_only

from . import tailnet
lazy from . import agent_devices
lazy from .agent_devices import DeviceStanding
lazy from .security import AGENT_SURFACES, Principal


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
    # An agent's token call: its client, where its device stands
    # (``agent_devices.DeviceStanding``), and who approved the device.
    agent: str = ""
    device_standing: str = ""
    approved_by: str = ""

    @property
    def session_principals(self) -> frozenset[str]:
        return frozenset(principal.strip().casefold() for principal in (self.username, self.email) if principal.strip())

    @property
    def route(self) -> str:
        """How this session was established, in a word; empty when the
        session does not say, and the row is left out."""

        if not self.signed_in_by:
            return ""
        return "single sign-on" if "OIDC" in self.signed_in_by else "password"

    @property
    def corroborated(self) -> bool:
        """Whether two independent systems agree on who is asking.

        The session says who signed in; the tailnet says which account owns the
        device the request came from. Neither consults the other, so agreement
        between them is independent corroboration. Different account
        namespaces require an explicit link; HQ does not infer equivalence
        from similar-looking names. A token call has no session, so the link
        is the person's approval of the device for that agent.
        """

        if self.agent:
            return self.device_standing == DeviceStanding.APPROVED
        tailnet = self.tailnet_user.strip().casefold()
        if not tailnet or not self.session_principals:
            return False
        if tailnet in self.session_principals:
            return True
        return tailnet == self.provider_principal.strip().casefold()

    @property
    def agreement_basis(self) -> str:
        if self.agent:
            return "device approval" if self.corroborated else ""
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
            not self.agent
            and separator
            and not self.corroborated
            and any(
                principal.rpartition("@")[2] == namespace for principal in self.session_principals if "@" in principal
            )
        )


def signed_in(request) -> Any:
    """The person this request's session signed in, or None.

    None for a request signed out, and for one no session reached: a request
    the MCP boundary admitted carries no ``user`` at all. Reading
    ``is_authenticated`` here also resolves Django's lazy user in the thread
    serving the request.
    """

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None
    return user


def identity_of(request, device: tailnet.Device | None, principal: Principal | None = None) -> Identity:
    """Who is asking: the session's person, or ``principal``'s device approval for an agent's token call."""

    if principal is not None and principal.interface in AGENT_SURFACES:
        return _agent(principal, device)
    user = signed_in(request)
    backends = tuple(settings.AUTHENTICATION_BACKENDS)
    session = getattr(request, "session", None)
    expiry = None
    if session is not None:
        try:
            expiry = session.get_expiry_date()
        except AttributeError, ValueError, TypeError:
            expiry = None
    signed_in_by = str((session or {}).get("_auth_user_backend", "") if session is not None else "")
    return Identity(
        **_person(user),
        sso_only=sso_only(),
        backends=backends,
        session_expires=expiry,
        tailnet_user=device.user if device else "",
        signed_in_by=signed_in_by,
        # Who vouched for the person, taken from the endpoint HQ actually sends
        # them to rather than from a name written down beside it. A configured
        # provider did not vouch for a password session, so it must not appear
        # beside that session as though it did.
        provider=_provider_host() if "OIDC" in signed_in_by else "",
        token_expires=str((session or {}).get("oidc_id_token_expiration", "") if session is not None else ""),
        provider_principal=(
            str((session or {}).get(TAILSCALE_PRINCIPAL_SESSION_KEY, ""))
            if session is not None and "OIDC" in signed_in_by
            else ""
        ),
    )


def _agent(principal: Principal, device: tailnet.Device | None) -> Identity:
    """An agent's call, as the record of the device it came from stands."""

    standing = agent_devices.standing(principal)
    record = agent_devices.record_of(principal) if standing is DeviceStanding.APPROVED else None
    approver = record.approved_by if record is not None else None
    return Identity(
        tailnet_user=device.user if device else "",
        agent=principal.actor,
        device_standing=standing.value,
        approved_by=approver.get_username() if approver is not None else "",
    )


def _person(user) -> dict[str, Any]:
    """What an identity says of the person signed in; nothing, so the defaults, for nobody."""

    if user is None:
        return {}
    return {
        "username": user.username,
        "email": user.email,
        "full_name": user.get_full_name().strip(),
        "groups": tuple(user.groups.values_list("name", flat=True)),
        "staff": user.is_staff,
        "superuser": user.is_superuser,
        "last_sign_in": user.last_login,
    }


def _provider_host() -> str:
    """The identity provider, named by where sign-in is actually sent."""

    endpoint = settings.OIDC_OP_AUTHORIZATION_ENDPOINT
    return urlsplit(endpoint).hostname or ""
