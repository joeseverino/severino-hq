"""Application-level principals and capability enforcement."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from django.conf import settings
from django.utils.http import url_has_allowed_host_and_scheme


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
    WRITE_PROJECTS = "write_projects"
    WRITE_ASSETS = "write_assets"
    WRITE_CONTENT = "write_content"
    WRITE_EXPENSES = "write_expenses"
    WRITE_RECEIPTS = "write_receipts"
    SYNC_DOCUMENTATION = "sync_documentation"
    WRITE_DOCUMENTATION = "write_documentation"
    PRUNE_DOCUMENTATION = "prune_documentation"
    DELETE_PROJECTS = "delete_projects"
    DELETE_ASSETS = "delete_assets"
    DELETE_CONTENT = "delete_content"
    DELETE_EXPENSES = "delete_expenses"
    DELETE_DOCUMENTATION = "delete_documentation"
    DELETE_RECEIPTS = "delete_receipts"
    MANAGE_INFRASTRUCTURE = "manage_infrastructure"
    REQUEST_CERTIFICATE_RENEWAL = "request_certificate_renewal"


class AuthorizationError(PermissionError):
    """Refused for want of a capability. Its message is written for the caller."""

    code = "forbidden"

    def __init__(self, reason: str = "", *args: object) -> None:
        super().__init__(reason, *args)
        self.reason = reason


# Stable core contract retained for callers constructing explicit principals.
# Runtime operator principals derive plugin grants in addition to this set.
OPERATOR_CAPABILITIES = frozenset(Capability)


@dataclass(frozen=True)
class Principal:
    actor: str
    interface: str
    capabilities: frozenset[Capability | str]

    def permits(self, *capabilities: Capability | str) -> bool:
        """Whether this principal holds every capability named.

        The question `require` answers by raising. Both exist because callers
        divide cleanly in two: a handler enforcing authority wants the
        exception, and a surface deciding whether to draw a control wants a
        boolean -- and a surface that has to catch an exception to render a
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
        available = {
            item.value if isinstance(item, Capability) else item for item in self.capabilities
        }
        if name not in available:
            raise AuthorizationError(
                f"{self.interface} principal {self.actor!r} lacks "
                f"{name!r}."
            )


def _operator_capabilities():
    from .plugins import plugin_capabilities

    return OPERATOR_CAPABILITIES | plugin_capabilities("operator")


def web_principal(user) -> Principal:
    if not getattr(user, "is_authenticated", False):
        raise AuthorizationError("An authenticated web operator is required.")
    return Principal(user.get_username(), "web", _operator_capabilities())


def cli_principal() -> Principal:
    return Principal("local-operator", "cli", _operator_capabilities())


def mcp_principal() -> Principal:
    from .plugins import plugin_capabilities

    capabilities = {Capability.READ}
    capabilities.update(plugin_capabilities("mcp_read"))
    # Mirroring the vault's documentation index is gated on its own, because it
    # is the one write an operator wants routinely and in isolation. Bundled
    # with the rest it could only be granted by also handing the service
    # account write access to expenses, receipts, projects, assets and content
    # -- so in practice it stayed off and the index silently fell behind.
    if getattr(settings, "SEVERINO_MCP_ENABLE_DOC_SYNC", False):
        capabilities.add(Capability.SYNC_DOCUMENTATION)
    if getattr(settings, "SEVERINO_MCP_ENABLE_WRITES", False):
        capabilities.update(
            {
                Capability.WRITE_PROJECTS,
                Capability.WRITE_ASSETS,
                Capability.WRITE_CONTENT,
                Capability.WRITE_EXPENSES,
                Capability.WRITE_RECEIPTS,
                # Broad writes still imply doc sync; the narrow flag exists to
                # grant it *without* them, not to withhold it from them.
                Capability.SYNC_DOCUMENTATION,
                Capability.WRITE_DOCUMENTATION,
            }
        )
        capabilities.update(plugin_capabilities("mcp_write"))
    if getattr(settings, "SEVERINO_MCP_ENABLE_PRUNE", False):
        capabilities.add(Capability.PRUNE_DOCUMENTATION)
    if getattr(
        settings, "SEVERINO_MCP_ENABLE_WRITES", False
    ) and getattr(settings, "SEVERINO_MCP_ENABLE_DELETES", False):
        capabilities.update(
            {
                Capability.DELETE_PROJECTS,
                Capability.DELETE_ASSETS,
                Capability.DELETE_CONTENT,
                Capability.DELETE_EXPENSES,
                Capability.DELETE_DOCUMENTATION,
                Capability.DELETE_RECEIPTS,
            }
        )
    # Topology sync needs to declare infrastructure; it never needs to ask a
    # certificate authority for anything. Bundling the two meant enabling
    # `hq sync` also handed the service account certificate renewal.
    if getattr(settings, "SEVERINO_MCP_ENABLE_INFRASTRUCTURE", False):
        capabilities.add(Capability.MANAGE_INFRASTRUCTURE)
    if getattr(settings, "SEVERINO_MCP_ENABLE_CERT_RENEWAL", False):
        capabilities.add(Capability.REQUEST_CERTIFICATE_RENEWAL)
    # A read, but an outbound one. It leaves the tailnet, spends somebody
    # else's rate limit, and tells a third party what HQ was asked about --
    # none of which a baseline read does, and none of which an unattended
    # caller should start doing because a capability was folded into READ.
    if getattr(settings, "SEVERINO_MCP_ENABLE_LOOKUP", False):
        capabilities.add(Capability.LOOK_UP_PUBLIC_RECORDS)
    return Principal("mcp-service-account", "mcp", frozenset(capabilities))


def safe_next(request, *, fallback: str = "", scope: str = "") -> str:
    """A caller-supplied destination, but only if it points back at us.

    Shared rather than repeated: the same "go back where I came from" appears
    on forms, on toggles and on anything else that returns somewhere, and each
    one written separately is one more chance to redirect wherever a query
    string says. Checked in one place, every caller gets the check -- adapters
    and plugins alike, which is why it sits beside the principals rather than
    in whichever app happened to need it first.

    ``scope`` narrows the destination to a path prefix, for callers that return
    somewhere within one section rather than anywhere on the host. ``fallback``
    is what an absent or rejected destination becomes, so a caller can redirect
    on the result unconditionally instead of re-deciding what "nowhere" means.
    """

    candidate = (
        request.POST.get("next", "") if request.method == "POST" else ""
    ) or request.GET.get("next", "")
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
