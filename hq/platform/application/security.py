"""Application-level principals and capability enforcement."""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

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
    # The audit trail is a security log. Free-text search over it is granted
    # apart from baseline reads, so a viewer and an agent granted only READ
    # never hold it implicitly.
    READ_AUDIT_LOG = "read_audit_log"
    # An outbound read, granted apart from baseline READ for the same reason:
    # it spends a third party's rate limit and discloses what HQ is asking
    # about, so an agent holds it only where its grant names it.
    LOOK_UP_PUBLIC_RECORDS = "look_up_public_records"
    # A record domain's write_<resource> and delete_<resource> are not listed
    # here: they derive from its ``Records`` declaration (``record_permissions``).
    # The operator's own calendar: appointments, visits, plans. Personal, so
    # an agent reads and writes it only where its grant names it.
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
    # Makes a scanned commit the playbooks HQ runs, or sets it aside. Held at
    # the operator's own interfaces: an agent may refresh, and a person decides
    # what of the repository HQ runs.
    ADOPT_PLAYBOOKS = "adopt_playbooks"
    # A person's own decisions, held only in the browser (``HELD_ONLY_AT``): no
    # credential approves what a credential asked for, or pauses or grants agents.
    DECIDE_APPROVALS = "decide_approvals"
    GOVERN_AGENTS = "govern_agents"
    # A person's own settings (their theme and calendar choices, and the
    # dashboard's shared settings), pins and set-aside action items. Held by a
    # principal with a person behind it (``NEEDS_A_PERSON``): a machine's token
    # is nobody's, so it has none of them to keep.
    SET_PREFERENCES = "set_preferences"
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
# The URL namespace the machine API's routes resolve under, which tells a
# request through that door from one through the web.
API_NAMESPACE = "hq_api"
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
# an agent: held to its token's grant (``agent_grant``), stopped by the
# Agents pause, and what it asked for is an agent's request. A token proves which client presented it
# and, for a delegated grant, which person allowed that client, but nothing in
# it says the client is that person's own device rather than an agent they
# authorized, so only the identity provider's device scope, on a client the
# person registered in HQ, earns a token a person's standing, as
# ``DEVICE_INTERFACE``.
AGENT_SURFACES = frozenset({MCP_INTERFACE, API_INTERFACE})

# Capabilities held only at these interfaces, whatever a grant says: the two
# that would let an agent lift its own limits, HQ's own back-channel logout,
# the uploads, whose bytes this keeps out of a hold, the audit log and an
# answer, and adopting playbooks, which decides what code HQ runs. An
# agent's token that names one is refused it in ``Principal.require``, the
# first thing the capability runner asks, so nothing it sends is held for
# approval, stored or logged.
HELD_ONLY_AT: dict[str, frozenset[str]] = {
    Capability.UPLOAD_CERTIFICATE: OPERATOR_INTERFACES,
    Capability.UPLOAD_RECEIPT: OPERATOR_INTERFACES,
    Capability.ADOPT_PLAYBOOKS: OPERATOR_INTERFACES,
    Capability.DECIDE_APPROVALS: INTERACTIVE_INTERFACES,
    Capability.GOVERN_AGENTS: INTERACTIVE_INTERFACES,
    Capability.END_SESSIONS: frozenset({INTERNAL_INTERFACE}),
}

# Capabilities that keep something of a person's, held only by a principal with
# a person behind it (``has_a_person``) on whatever interface it calls. An agent
# a person signed in may be granted them and acts on that person's own; a
# machine's token, the host's shell and HQ itself are nobody's, so
# ``Principal.require`` refuses them one whatever their grant says, and the
# catalog lists it to them as not permitted.
NEEDS_A_PERSON: frozenset[str] = frozenset(
    {Capability.SET_PREFERENCES, Capability.PIN_FAVORITES, Capability.SET_ASIDE_ACTION_ITEMS}
)


def is_interactive(principal: Principal) -> bool:
    """Whether this act is a person's, rather than a credential's."""

    return principal.interface in INTERACTIVE_INTERFACES


def has_a_person(principal: Principal) -> bool:
    """Whether a person stands behind ``principal``: a web session's or a device's, or a token's a person signed in.

    The person (``person_of``) is a web session's actor, and otherwise the
    sign-in a token carried (``Principal.person_key``), which a device always
    has and an agent has when a person signed it in.
    """

    return is_interactive(principal) or bool(principal.person_key)


class AuthorizationError(PermissionError):
    """Refused for want of a capability. Its message is written for the caller."""

    code = "forbidden"

    def __init__(self, reason: str = "", *args: object) -> None:
        super().__init__(reason, *args)
        self.reason = reason


class InsufficientScope(AuthorizationError):
    """Refused because the token did not carry ``scope``, a permission its person
    holds and the identity provider could grant: a new sign-in gets it through.

    RFC 6750's ``insufficient_scope``, on both doors. A refusal a new sign-in
    cannot fix (the person's role, ``HELD_ONLY_AT``, HQ's own rules) stays a
    plain ``AuthorizationError`` or a ``PolicyDenied``.
    """

    code = "insufficient_scope"

    def __init__(self, reason: str, scope: str) -> None:
        super().__init__(reason)
        self.scope = scope


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
    # machine's own token and for principals HQ constructs itself. It names
    # the person (``person_of``) whose own settings, pins and set-asides the
    # token acts on, a device's or an agent's alike.
    person_key: str = ""
    # What the token lacks that a new sign-in could add: each permission a
    # token could carry (``agent_grantable``) that this principal's person
    # holds and its grant left out. Empty for principals HQ constructs itself.
    grantable: frozenset[str] = frozenset()
    # The tailnet device a token's call came from, by Tailscale's stable id,
    # as its admission placed it (``agent_devices.place``); empty where HQ
    # could not place the call and for principals HQ constructs itself.
    device: str = ""

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
        # Asked first, since no grant or new sign-in gives a machine a person.
        if name in NEEDS_A_PERSON and not has_a_person(self):
            raise AuthorizationError(
                f"{name!r} needs a signed-in account, and the {self.interface} principal {self.actor!r} "
                "is a machine token with none. Sign the agent in through a person's account."
            )
        # A Capability is a StrEnum, equal to and hashed as its value, so the
        # held set answers for a name as it stands.
        if name in self.grantable:
            raise InsufficientScope(
                f"{self.interface} principal {self.actor!r} lacks {name!r}: its token does not carry it. "
                f"Grant {name!r} to the client {self.actor!r} in Pocket ID, then sign in again.",
                name,
            )
        if name not in self.capabilities:
            raise AuthorizationError(f"{self.interface} principal {self.actor!r} lacks {name!r}.")
        if not could_hold(self.interface, (name,)):
            where = " or ".join(sorted(HELD_ONLY_AT[name]))
            raise AuthorizationError(f"{name!r} is held only through {where}, never by a {self.interface} principal.")


def could_ever_hold(principal: Principal, capabilities: Iterable[Capability | str]) -> bool:
    """Whether ``principal``, granted anything, could hold every one of ``capabilities``.

    ``could_hold`` for its interface, and ``NEEDS_A_PERSON`` for whether a
    person stands behind it: what ``Principal.require`` refuses whatever the
    grant, so a catalog that lists only what its asker could run asks it.
    """

    needed = frozenset(str(capability) for capability in capabilities)
    return could_hold(principal.interface, needed) and (has_a_person(principal) or NEEDS_A_PERSON.isdisjoint(needed))


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


class Requires:
    """What a spec that names the capabilities it needs says them as: a tuple, however it declared them."""

    __slots__ = ()
    required_capability: Capability | str | tuple[Capability | str, ...]

    @property
    def required_capabilities(self) -> tuple[Capability | str, ...]:
        if isinstance(self.required_capability, tuple):
            return self.required_capability
        return (self.required_capability,)


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
    """The signed-in person a principal stands for (``has_a_person``); None for one that is nobody's."""

    if not has_a_person(principal):
        return None
    users = get_user_model()
    if principal.interface == WEB_INTERFACE:
        return users._default_manager.filter(**{users.USERNAME_FIELD: principal.actor}).first()
    # A token's actor is its client, so its person is whoever signed in as the
    # token's subject.
    key = principal.person_key
    return users._default_manager.filter(linked_accounts__provider=SIGN_IN, linked_accounts__login=key).first()


def person(principal: Principal) -> AbstractBaseUser:
    """The signed-in person a principal is, for a use case that keeps something of theirs."""

    if not has_a_person(principal):
        raise AuthorizationError(
            f"This needs a signed-in account, and a {principal.interface} principal is a machine token with none."
        )
    found = person_of(principal)
    if found is None:
        raise RecordNotFound("No HQ account matches this sign-in.")
    return found


def internal_principal(purpose: str) -> Principal:
    """HQ reading its own records, never on a caller's behalf."""

    return Principal(purpose, INTERNAL_INTERFACE, operator_capabilities())


def cli_principal() -> Principal:
    return Principal(CLI_ACTOR, CLI_INTERFACE, operator_capabilities())


def is_agent(interface: str) -> bool:
    """Whether a principal at ``interface``, or a request made there, is an agent's."""

    return interface in AGENT_SURFACES


# Between a client id and the sign-in key of its person in a subject. The key
# is a hex digest, so the subject is read apart from its last separator; a
# machine's client id holding one is told apart by its row
# (``agent_registry.identity_of``).
_SUBJECT_SEPARATOR = "@"


def agent_subject(principal: Principal) -> str:
    """The unit HQ answers for: one client as one person, or a machine's client alone.

    What a rule, a block, a retry key, a rate and a held ask are kept against,
    so one person's consent, answers and allowance are never another's who
    signed the same client in.
    """

    return subject_of(principal.actor, principal.person_key)


def subject_of(client_id: str, person_key: str) -> str:
    """The subject of ``client_id`` as the person ``person_key`` names, or of the client alone."""

    return client_id if not person_key else f"{client_id}{_SUBJECT_SEPARATOR}{person_key}"


def subject_parts(subject: str) -> tuple[str, str]:
    """A subject back to its client id and its person's sign-in key ("" for a machine's)."""

    client_id, separator, key = subject.rpartition(_SUBJECT_SEPARATOR)
    return (client_id, key) if separator else (subject, "")


def agent_principal(actor: str, interface: str, granted: frozenset[str], *, person_key: str = "") -> Principal:
    """An agent holding its identity provider's grant, within that person's role
    for a token a person signed in.

    The one way a token becomes a principal, on every surface a token reaches,
    so the cap belongs to the agent rather than to the door it called through.
    """

    if not is_agent(interface):
        raise ValueError(f"{interface!r} is not an interface an agent reaches HQ through.")
    held = _held_by_person(person_key) if person_key else None
    capabilities = agent_grant(granted, held)
    return Principal(
        actor,
        interface,
        capabilities,
        granted=granted,
        person_key=person_key,
        grantable=_within(agent_grantable(), held) - capabilities,
    )


def agent_grantable() -> frozenset[str]:
    """Every permission an agent's token could carry and hold: what HQ and its
    extensions grant, less what ``HELD_ONLY_AT`` keeps from the agent doors.

    The one filter for what a token is held to (``agent_grant``), what a sign-in
    asks for (``scopes_supported``) and what a refusal tells a person to grant.
    """

    return frozenset(
        str(capability)
        for capability in operator_capabilities()
        if all(could_hold(surface, (capability,)) for surface in AGENT_SURFACES)
    )


def agent_grant(granted: Iterable[str], held: frozenset[Capability | str] | None = None) -> frozenset[str]:
    """What of an agent's ``granted`` is in force: what HQ grants that an agent's
    interface could hold, and within ``held``, what its person holds, for a
    token a person signed in.

    The one rule, for the principal a token becomes and the Agents page that shows it.
    """

    return _within(frozenset(granted) & agent_grantable(), held)


def _within(names: frozenset[str], held: frozenset[Capability | str] | None) -> frozenset[str]:
    """``names`` within what a person holds, or all of them for a token no person signed in."""

    return names if held is None else names & {str(capability) for capability in held}


def _held_by_person(person_key: str) -> frozenset[Capability | str]:
    """What the person a token was signed in by holds, refusing the token when that is nobody with a role."""

    held = roles.held_by(person_key)
    if held is None:
        raise AuthorizationError(
            "The account that signed this token in has no role in HQ. "
            "Sign in to HQ once with it, as a member of a group HQ gives a role."
        )
    return held


def device_principal(
    actor: str, capabilities: frozenset[str], granted: frozenset[str], *, person_key: str
) -> Principal:
    """A person's own device, holding the capabilities its token carries within its person's role.

    ``actor`` is the device's client, so the audit log names the device and
    revoking that one client in the identity provider ends it. ``capabilities`` is the token's grant without the scope that
    marked it a device, so the standing widens nothing; ``granted`` is the
    whole grant, for the Agents page.
    """

    role = _held_by_person(person_key)
    held = capabilities & role
    return Principal(
        actor,
        DEVICE_INTERFACE,
        held,
        granted=granted,
        person_key=person_key,
        grantable=_within(agent_grantable(), role) - held,
    )


def mcp_principal() -> Principal:
    """An agent holding everything a token could grant it, for a caller exercising agent behavior.

    It is not an identity anyone authenticates as.
    """

    return Principal("mcp-service-account", MCP_INTERFACE, agent_grant(operator_capabilities()))


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
