"""Application-level principals and capability enforcement."""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from django.conf import settings
from django.utils.http import url_has_allowed_host_and_scheme
lazy from django.contrib.auth import get_user_model
lazy from django.contrib.auth.base_user import AbstractBaseUser
lazy from django.contrib.auth.models import AnonymousUser
lazy from django.http import HttpRequest

lazy from .domains import host_records
lazy from .plugins import plugin_capabilities
lazy from .record_errors import RecordNotFound


class Capability(StrEnum):
    READ = "read"
    # The audit trail is a security log. Free-text search over it is gated
    # separately from baseline reads so a least-privilege adapter principal
    # (e.g. MCP) never gets it implicitly.
    READ_AUDIT_LOG = "read_audit_log"
    # An outbound read, gated apart from baseline READ for the same reason
    # READ_AUDIT_LOG is: `mcp_principal` holds READ unconditionally, so folding
    # this into it would let the machine account spend a third party's rate
    # limit and disclose what HQ is asking about, with nobody having decided
    # that. Operators hold every capability; MCP holds this one only if a
    # deployment says so.
    LOOK_UP_PUBLIC_RECORDS = "look_up_public_records"
    # A record domain's write_<resource> and delete_<resource> are not listed
    # here: they derive from its ``Records`` declaration (``record_permissions``).
    # The operator's own calendar: appointments, visits, plans. Personal, so
    # an agent reads and writes it only on its own switch.
    READ_CALENDAR = "read_calendar"
    WRITE_CALENDAR = "write_calendar"
    MANAGE_CONTACTS = "manage_contacts"
    SYNC_DOCUMENTATION = "sync_documentation"
    PRUNE_DOCUMENTATION = "prune_documentation"
    DELETE_CALENDAR = "delete_calendar"
    MANAGE_INFRASTRUCTURE = "manage_infrastructure"
    REQUEST_CERTIFICATE_RENEWAL = "request_certificate_renewal"
    # Hands HQ a private key. Held only at the operator's own interfaces
    # (``HELD_ONLY_AT``), so no token carries one in, whatever its grant says.
    UPLOAD_CERTIFICATE = "upload_certificate"
    # Hands HQ a receipt's file. Held at the operator's own interfaces as a
    # certificate is: a file arrives through a person, never a token.
    UPLOAD_RECEIPT = "upload_receipt"
    # A person's own decisions and settings, held only in the browser
    # (``HELD_ONLY_AT``): no credential approves what a credential asked for,
    # pauses or grants agents, or arranges somebody's pages.
    DECIDE_APPROVALS = "decide_approvals"
    GOVERN_AGENTS = "govern_agents"
    SET_PREFERENCES = "set_preferences"
    # A person's own pins and set-aside action items. Held only by the person
    # (``HELD_ONLY_AT``): a token is nobody's, so it has no favorites to keep.
    PIN_FAVORITES = "pin_favorites"
    SET_ASIDE_ACTION_ITEMS = "set_aside_action_items"


# The interfaces where a request is a person, established as one and present at
# the time. The web surface is behind an identity provider and a passkey, so a
# click there is somebody; a token call is a credential, and a credential is
# whatever currently holds it.
#
# An allowlist rather than a list of the machine interfaces, so an interface
# added later is treated as a credential until somebody decides otherwise. The
# other order fails open: a new adapter would inherit a person's standing by
# not having been thought about.
INTERACTIVE_INTERFACES = frozenset({"web"})


# Operators: a person signed in to the web, or at the host's own shell.
OPERATOR_INTERFACES = frozenset({"web", "cli"})

# Capabilities held only at these interfaces, whatever a grant or a ceiling
# says. A token that names one is refused it in ``Principal.require``, the
# first thing the capability runner asks, so nothing it sends is held for
# approval, stored or logged.
HELD_ONLY_AT: dict[str, frozenset[str]] = {
    Capability.UPLOAD_CERTIFICATE: OPERATOR_INTERFACES,
    Capability.UPLOAD_RECEIPT: OPERATOR_INTERFACES,
    Capability.DECIDE_APPROVALS: INTERACTIVE_INTERFACES,
    Capability.GOVERN_AGENTS: INTERACTIVE_INTERFACES,
    Capability.SET_PREFERENCES: INTERACTIVE_INTERFACES,
    Capability.PIN_FAVORITES: INTERACTIVE_INTERFACES,
    Capability.SET_ASIDE_ACTION_ITEMS: INTERACTIVE_INTERFACES,
}


def is_interactive(principal: Principal) -> bool:
    """Whether this act is a person's, rather than a credential's."""

    return principal.interface in INTERACTIVE_INTERFACES


class AuthorizationError(PermissionError):
    """Refused for want of a capability. Its message is written for the caller."""

    code = "forbidden"

    def __init__(self, reason: str = "", *args: object) -> None:
        super().__init__(reason, *args)
        self.reason = reason


class PolicyDenied(AuthorizationError):
    """Refused by operator policy rather than a missing grant."""

    code = "denied_by_policy"


def record_permissions(kind: str) -> frozenset[str]:
    """Every record domain's ``write`` or ``delete`` permission, by declaration."""

    return frozenset(getattr(records, kind) for records in host_records())


def host_capabilities() -> frozenset[Capability | str]:
    """Everything the host can grant: its own capabilities and every record domain's."""

    return frozenset(Capability) | record_permissions("write") | record_permissions("delete")


# The host's grants alone, for callers constructing explicit principals that
# exercise host behavior. A principal meant to hold every grant, extensions'
# included, takes ``operator_capabilities()``.
OPERATOR_CAPABILITIES = host_capabilities()


@dataclass(frozen=True, slots=True)
class Principal:
    actor: str
    interface: str
    capabilities: frozenset[Capability | str]
    # The person who agreed to this act, where one had to. Empty for everything
    # else, which is almost everything: an operator clicking a button is their
    # own consent. It is carried on the principal rather than passed alongside
    # it so a replayed request keeps naming whoever asked for it, and the
    # record of who allowed it travels with the act instead of being looked up
    # afterwards from two rows that nothing joins.
    approved_by: str = ""
    # The identity provider's grant before any deployment cap; empty for
    # principals HQ constructs itself.
    granted: frozenset[str] = frozenset()

    def permits(self, *capabilities: Capability | str) -> bool:
        """Whether this principal holds every capability named.

        The question `require` answers by raising. Both exist because callers
        divide cleanly in two: a handler enforcing authority wants the
        exception, and a surface deciding whether to draw a control wants a
        boolean, and a surface that has to catch an exception to render a
        menu ends up catching it in more places than it should.
        """

        try:
            for capability in capabilities:
                self.require(capability)
        except AuthorizationError:
            return False
        return True

    def require(self, capability: Capability | str) -> None:
        name = capability.value if isinstance(capability, Capability) else capability
        available = {item.value if isinstance(item, Capability) else item for item in self.capabilities}
        if name not in available:
            raise AuthorizationError(f"{self.interface} principal {self.actor!r} lacks {name!r}.")
        if not could_hold(self.interface, (name,)):
            where = " or ".join(sorted(HELD_ONLY_AT[name]))
            raise AuthorizationError(f"{name!r} is held only through {where}, never by a {self.interface} principal.")


def could_hold(interface: str, capabilities: Iterable[Capability | str]) -> bool:
    """Whether a principal on ``interface`` could ever hold every one of ``capabilities``.

    The one reading of ``HELD_ONLY_AT``: a refusal at run time, a catalog that
    lists only what its asker could run, and the agent policy page that offers
    a rule only where an agent could use it all ask it.
    """

    for capability in capabilities:
        interfaces = HELD_ONLY_AT.get(capability)
        if interfaces is not None and interface not in interfaces:
            return False
    return True


def require_all(principal: Principal, capabilities: Iterable[Capability | str]) -> None:
    """``Principal.require`` for a set: raise on the first capability it lacks.

    The one authorization rule every registry applies to what a spec requires.
    """

    for capability in capabilities:
        principal.require(capability)


def operator_capabilities() -> frozenset[Capability | str]:
    """Everything an operator holds: the host's grants and every installed extension's.

    The most any principal is granted, since an extension's MCP grants are a
    subset of its operator's (``plugins._validate_capabilities``).
    """

    return host_capabilities() | plugin_capabilities("operator")


def web_principal(user: AbstractBaseUser | AnonymousUser) -> Principal:
    if not user.is_authenticated:
        raise AuthorizationError("An authenticated web operator is required.")
    return Principal(user.get_username(), "web", operator_capabilities())


def person_of(principal: Principal) -> AbstractBaseUser | None:
    """The signed-in person an interactive principal stands for; None for a credential."""

    if not is_interactive(principal):
        return None
    users = get_user_model()
    return users._default_manager.filter(**{users.USERNAME_FIELD: principal.actor}).first()


def person(principal: Principal) -> AbstractBaseUser:
    """The signed-in person a principal is, for a use case that keeps something of theirs."""

    if not is_interactive(principal):
        raise AuthorizationError(f"A {principal.interface} principal is nobody's, so it keeps nothing of a person's.")
    found = person_of(principal)
    if found is None:
        raise RecordNotFound("That person was not found.")
    return found


def internal_principal(purpose: str) -> Principal:
    """HQ reading its own records, never on a caller's behalf."""

    return Principal(purpose, "internal", operator_capabilities())


def cli_principal() -> Principal:
    return Principal("local-operator", "cli", operator_capabilities())


def mcp_principal() -> Principal:
    """The most any MCP caller may do on this deployment: the ceiling.

    Built from the `SEVERINO_MCP_ENABLE_*` switches and intersected with each
    agent's own grant, so an agent never exceeds either. It is not an identity
    anyone authenticates as.
    """

    capabilities: set[Capability | str] = {Capability.READ}
    capabilities.update(plugin_capabilities("mcp_read"))
    # Mirroring the vault's documentation index is gated on its own, because it
    # is the one write an operator wants routinely and in isolation. Bundled
    # with the rest it could only be granted by also handing the service
    # account write access to expenses, receipts, projects, assets and content
    # so in practice it stayed off and the index silently fell behind.
    if settings.SEVERINO_MCP_ENABLE_DOC_SYNC:
        capabilities.add(Capability.SYNC_DOCUMENTATION)
    if settings.SEVERINO_MCP_ENABLE_WRITES:
        capabilities.update(record_permissions("write"))
        # Broad writes still imply doc sync; the narrow flag exists to grant it
        # *without* them, not to withhold it from them.
        capabilities.add(Capability.SYNC_DOCUMENTATION)
        capabilities.update(plugin_capabilities("mcp_write"))
    # The calendar is personal, so it is neither in READ nor in the broad
    # writes: "put this on my calendar" is its own decision, and granting it
    # grants nothing else.
    if settings.SEVERINO_MCP_ENABLE_CALENDAR:
        capabilities.update({Capability.READ_CALENDAR, Capability.WRITE_CALENDAR})
    if settings.SEVERINO_MCP_ENABLE_PRUNE:
        capabilities.add(Capability.PRUNE_DOCUMENTATION)
    if settings.SEVERINO_MCP_ENABLE_WRITES and settings.SEVERINO_MCP_ENABLE_DELETES:
        capabilities.update(record_permissions("delete"))
        if settings.SEVERINO_MCP_ENABLE_CALENDAR:
            capabilities.add(Capability.DELETE_CALENDAR)
    # Topology sync needs to declare infrastructure; it never needs to ask a
    # certificate authority for anything, so enabling `hq sync` does not grant
    # the service account certificate renewal.
    if settings.SEVERINO_MCP_ENABLE_INFRASTRUCTURE:
        capabilities.add(Capability.MANAGE_INFRASTRUCTURE)
    if settings.SEVERINO_MCP_ENABLE_CERT_RENEWAL:
        capabilities.add(Capability.REQUEST_CERTIFICATE_RENEWAL)
    # A read, but an outbound one. It leaves the tailnet, spends somebody
    # else's rate limit, and tells a third party what HQ was asked about,
    # none of which a baseline read does, and none of which an unattended
    # caller should start doing because a capability was folded into READ.
    if settings.SEVERINO_MCP_ENABLE_LOOKUP:
        capabilities.add(Capability.LOOK_UP_PUBLIC_RECORDS)
    return Principal("mcp-service-account", "mcp", frozenset(capabilities))


def safe_next(request: HttpRequest, *, fallback: str = "", scope: str = "") -> str:
    """A caller-supplied destination, but only if it points back at us.

    The one check every "go back where I came from" passes, adapters and
    plugins alike, so none redirects wherever a query string says. ``scope``
    narrows it to a path prefix; ``fallback`` is what an absent or rejected
    destination becomes, so a caller redirects on the result unconditionally.
    """

    candidate = (request.POST.get("next", "") if request.method == "POST" else "") or request.GET.get("next", "")
    candidate = candidate.strip()
    if (
        candidate
        and (not scope or candidate.startswith(scope))
        and url_has_allowed_host_and_scheme(
            candidate,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        )
    ):
        return candidate
    return fallback
