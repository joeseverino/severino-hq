"""Application-level principals and capability enforcement."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from django.conf import settings


class Capability(StrEnum):
    READ = "read"
    # The audit trail is a security log. Free-text search over it is gated
    # separately from baseline reads so a least-privilege adapter principal
    # (e.g. MCP) never gets it implicitly.
    READ_AUDIT_LOG = "read_audit_log"
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
    code = "forbidden"


# Stable core contract retained for callers constructing explicit principals.
# Runtime operator principals derive plugin grants in addition to this set.
OPERATOR_CAPABILITIES = frozenset(Capability)


@dataclass(frozen=True)
class Principal:
    actor: str
    interface: str
    capabilities: frozenset[Capability | str]

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


def mobile_principal(user) -> Principal:
    """A registered native client acting as the operator who authenticated it.

    Same authority as the browser session it was established from, on purpose.
    A phone that could only do less would send the operator back to a laptop
    for precisely the work a phone is best at -- capture in the moment -- and
    the narrower grant would be undone the first time that got annoying.

    The separate interface label is the point: every event a phone causes is
    attributable to a phone in the audit log, and a device is revocable on its
    own without touching the operator's account.
    """

    if not getattr(user, "is_authenticated", False):
        raise AuthorizationError("An authenticated operator is required.")
    return Principal(user.get_username(), "mobile", _operator_capabilities())


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
    return Principal("mcp-service-account", "mcp", frozenset(capabilities))
