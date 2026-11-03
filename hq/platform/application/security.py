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

lazy from . import roles
lazy from .domains import host_records
lazy from .linked_accounts import SIGN_IN
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
    # Ends a person's sessions when the identity provider signs them out. Held
    # only by HQ itself (``HELD_ONLY_AT``): the provider's signed logout token
    # is its credential, so no person or token signs anyone out through it.
    END_SESSIONS = "end_sessions"


# Every interface a principal or a record names, declared once here, so a
# comparison or a record elsewhere reads the constant and a misspelling fails
# to import rather than to match.
#
# A person signed in to the web interface.
WEB_INTERFACE = "web"
# The host's own shell: the operator's management commands.
CLI_INTERFACE = "cli"
# Who acts at the shell: whoever holds the host's account.
CLI_ACTOR = "local-operator"
# The agent doors: the MCP server and the machine API.
MCP_INTERFACE = "mcp"
API_INTERFACE = "api"
# What the MCP mount names each request, until a tool binds an operation of its own.
MCP_OPERATION = "mcp"
# Work HQ runs from its job queue, attributed to whoever started it.
JOB_INTERFACE = "job"
# The controller, reporting what it saw or applied on its own schedule.
CONTROLLER_INTERFACE = "controller"

# The interface of a person's own device (a phone app, a Shortcut), on
# whichever surface it called: a token whose client an administrator marked as
# one in the identity provider and the person registered as theirs in HQ. It stands as that person, with their browser's
# authority bounded by the token's grant; what keeps it safe is that every act
# names the device's client and revoking that client ends it alone
# (docs/Security.md#agents).
DEVICE_INTERFACE = "device"

# The interfaces where a request is a person's. The web surface is behind an
# identity provider and a passkey, so a click there is somebody, and a device is
# that person's own, so it holds whatever the browser holds. Any other token
# call is a credential, and a credential is whatever currently holds it.
#
# An allowlist rather than a list of the machine interfaces, so an interface
# added later is treated as a credential until somebody decides otherwise. The
# other order fails open: a new adapter would inherit a person's standing by
# not having been thought about.
INTERACTIVE_INTERFACES = frozenset({WEB_INTERFACE, DEVICE_INTERFACE})


# Operators: a person, or the host's own shell.
OPERATOR_INTERFACES = INTERACTIVE_INTERFACES | {CLI_INTERFACE}

# The interfaces with no thread that outlives the request: work asked for
# there runs to its end before the answer, since nothing would be left to run it.
WAITING_INTERFACES = frozenset({CLI_INTERFACE})

# HQ reading its own records, on nobody's behalf (``internal_principal``).
INTERNAL_INTERFACE = "internal"

# The interfaces an agent's token reaches HQ through. Every principal there is
# an agent: capped by ``agent_ceiling``, stopped by the Agents pause, and what
# it asked for is an agent's request. A token proves which client presented it
# and, for a delegated grant, which person allowed that client, but nothing in
# it says the client is that person's own device rather than an agent they
# authorized, so only the identity provider's device scope, on a client the
# person registered in HQ, earns a token a person's standing, as
# ``DEVICE_INTERFACE``.
AGENT_SURFACES = frozenset({MCP_INTERFACE, API_INTERFACE})

# Capabilities held only at these interfaces, whatever a grant or a ceiling
# says. An agent's token that names one is refused it in ``Principal.require``, the
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
    Capability.END_SESSIONS: frozenset({INTERNAL_INTERFACE}),
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
    # The sign-in key (``linked_accounts.sign_in_subject``) of the person a
    # token was signed in by, on a grant a person allowed; empty for a
    # machine's own token and for principals HQ constructs itself. It names a
    # person only for a device (``person_of``): an agent a person signed in
    # stands for nobody.
    person_key: str = ""

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
        # A Capability is a StrEnum, equal to and hashed as its value, so the
        # held set answers for a name as it stands.
        if name not in self.capabilities:
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
    subset of its operator's (``plugins.MANIFEST_CHECKS``).
    """

    return host_capabilities() | plugin_capabilities("operator")


def web_principal(user: AbstractBaseUser | AnonymousUser) -> Principal:
    """A signed-in person in the browser, holding their role's capabilities and nothing without one."""

    if not user.is_authenticated:
        raise AuthorizationError("An authenticated web operator is required.")
    return Principal(user.get_username(), WEB_INTERFACE, roles.grants(roles.role_of(user)))


def person_of(principal: Principal) -> AbstractBaseUser | None:
    """The signed-in person an interactive principal stands for; None for a credential."""

    if not is_interactive(principal):
        return None
    users = get_user_model()
    if principal.interface == DEVICE_INTERFACE:
        # A device's actor is its client, so its person is whoever signed in
        # as the token's subject, and a device principal without one is nobody.
        if not isinstance(principal, DevicePrincipal) or not principal.person_key:
            return None
        key = principal.person_key
        return users._default_manager.filter(linked_accounts__provider=SIGN_IN, linked_accounts__login=key).first()
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

    return Principal(purpose, INTERNAL_INTERFACE, operator_capabilities())


def cli_principal() -> Principal:
    return Principal(CLI_ACTOR, CLI_INTERFACE, operator_capabilities())


def is_agent(interface: str) -> bool:
    """Whether a principal at ``interface``, or a request made there, is an agent's."""

    return interface in AGENT_SURFACES


def agent_principal(actor: str, interface: str, granted: frozenset[str], *, person_key: str = "") -> Principal:
    """An agent holding its identity provider's grant, capped by ``agent_ceiling``
    and, for a token a person signed in, by that person's role.

    The one way a token becomes a principal, on every surface a token reaches,
    so the cap belongs to the agent rather than to the door it called through.
    """

    if not is_agent(interface):
        raise ValueError(f"{interface!r} is not an interface an agent reaches HQ through.")
    capabilities = agent_grant(granted, _held_by_person(person_key) if person_key else None)
    return Principal(actor, interface, capabilities, granted=granted, person_key=person_key)


def agent_grant(granted: Iterable[str], held: frozenset[Capability | str] | None = None) -> frozenset[str]:
    """What of an agent's ``granted`` is in force: within ``agent_ceiling``, and within
    ``held``, what its person holds, for a token a person signed in.

    The one rule, for the principal a token becomes and the Agents page that shows it.
    """

    capabilities = frozenset(granted) & agent_ceiling()
    return capabilities if held is None else capabilities & {str(capability) for capability in held}


def _held_by_person(person_key: str) -> frozenset[Capability | str]:
    """What the person a token was signed in by holds, refusing the token when that is nobody with a role."""

    held = roles.held_by(person_key)
    if held is None:
        raise AuthorizationError(
            "The person who signed this token in holds no role in HQ, so it can act for nobody. "
            "They sign in to HQ once, as a member of a group HQ gives a role."
        )
    return held


@dataclass(frozen=True, slots=True)
class DevicePrincipal(Principal):
    """A person's own device: the ``Principal`` whose ``person_key`` ``person_of``
    reads, since the actor is the device's client and names nobody."""


def device_principal(
    actor: str, capabilities: frozenset[str], granted: frozenset[str], *, person_key: str
) -> DevicePrincipal:
    """A person's own device, holding the capabilities its token carries within its person's role.

    No agent ceiling applies. ``actor`` is the device's client, so the audit
    log names the device and revoking that one client in the identity provider
    ends it. ``capabilities`` is the token's grant without the scope that
    marked it a device, so the standing widens nothing; ``granted`` is the
    whole grant, for the Agents page.
    """

    held = capabilities & _held_by_person(person_key)
    return DevicePrincipal(actor, DEVICE_INTERFACE, held, granted=granted, person_key=person_key)


def mcp_principal() -> Principal:
    """An agent holding the whole ceiling, for a caller exercising agent behavior.

    It is not an identity anyone authenticates as.
    """

    return Principal("mcp-service-account", MCP_INTERFACE, agent_ceiling())


def agent_ceiling() -> frozenset[str]:
    """The most any agent may do on this deployment, on every surface.

    Built from the `SEVERINO_AGENT_ENABLE_*` switches and intersected with each
    agent's own grant (``agent_principal``), so an agent never exceeds either.
    """

    # A viewer's reads, which every agent may do.
    capabilities: set[Capability | str] = set(roles.viewer_capabilities())
    # Mirroring the vault's documentation index is gated on its own, because it
    # is the one write an operator wants routinely and in isolation. Bundled
    # with the rest it could only be granted by also handing the service
    # account write access to expenses, receipts, projects, assets and content
    # so in practice it stayed off and the index silently fell behind.
    if settings.SEVERINO_AGENT_ENABLE_DOC_SYNC:
        capabilities.add(Capability.SYNC_DOCUMENTATION)
    if settings.SEVERINO_AGENT_ENABLE_WRITES:
        capabilities.update(record_permissions("write"))
        # Broad writes still imply doc sync; the narrow flag exists to grant it
        # *without* them, not to withhold it from them.
        capabilities.add(Capability.SYNC_DOCUMENTATION)
        capabilities.update(plugin_capabilities("mcp_write"))
    # The calendar is personal, so it is neither in READ nor in the broad
    # writes: "put this on my calendar" is its own decision, and granting it
    # grants nothing else.
    if settings.SEVERINO_AGENT_ENABLE_CALENDAR:
        capabilities.update({Capability.READ_CALENDAR, Capability.WRITE_CALENDAR})
    if settings.SEVERINO_AGENT_ENABLE_PRUNE:
        capabilities.add(Capability.PRUNE_DOCUMENTATION)
    if settings.SEVERINO_AGENT_ENABLE_WRITES and settings.SEVERINO_AGENT_ENABLE_DELETES:
        capabilities.update(record_permissions("delete"))
        if settings.SEVERINO_AGENT_ENABLE_CALENDAR:
            capabilities.add(Capability.DELETE_CALENDAR)
    # Topology sync needs to declare infrastructure; it never needs to ask a
    # certificate authority for anything, so enabling `hq sync` does not grant
    # the service account certificate renewal.
    if settings.SEVERINO_AGENT_ENABLE_INFRASTRUCTURE:
        capabilities.add(Capability.MANAGE_INFRASTRUCTURE)
    if settings.SEVERINO_AGENT_ENABLE_CERT_RENEWAL:
        capabilities.add(Capability.REQUEST_CERTIFICATE_RENEWAL)
    # A read, but an outbound one. It leaves the tailnet, spends somebody
    # else's rate limit, and tells a third party what HQ was asked about,
    # none of which a baseline read does, and none of which an unattended
    # caller should start doing because a capability was folded into READ.
    if settings.SEVERINO_AGENT_ENABLE_LOOKUP:
        capabilities.add(Capability.LOOK_UP_PUBLIC_RECORDS)
    return frozenset(str(capability) for capability in capabilities)


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
