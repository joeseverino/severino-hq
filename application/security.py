"""Application-level principals and capability enforcement."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from django.conf import settings


class Capability(StrEnum):
    READ = "read"
    WRITE_PROJECTS = "write_projects"
    WRITE_ASSETS = "write_assets"
    WRITE_CONTENT = "write_content"
    SYNC_DOCUMENTATION = "sync_documentation"
    PRUNE_DOCUMENTATION = "prune_documentation"


class AuthorizationError(PermissionError):
    code = "forbidden"


@dataclass(frozen=True)
class Principal:
    actor: str
    interface: str
    capabilities: frozenset[Capability]

    def require(self, capability: Capability) -> None:
        if capability not in self.capabilities:
            raise AuthorizationError(
                f"{self.interface} principal {self.actor!r} lacks "
                f"{capability.value!r}."
            )


OPERATOR_CAPABILITIES = frozenset(Capability)


def web_principal(user) -> Principal:
    if not getattr(user, "is_authenticated", False):
        raise AuthorizationError("An authenticated web operator is required.")
    return Principal(user.get_username(), "web", OPERATOR_CAPABILITIES)


def cli_principal() -> Principal:
    return Principal("local-operator", "cli", OPERATOR_CAPABILITIES)


def mcp_principal() -> Principal:
    capabilities = {Capability.READ}
    if getattr(settings, "SEVERINO_MCP_ENABLE_WRITES", False):
        capabilities.update(
            {
                Capability.WRITE_PROJECTS,
                Capability.WRITE_ASSETS,
                Capability.WRITE_CONTENT,
                Capability.SYNC_DOCUMENTATION,
            }
        )
    if getattr(settings, "SEVERINO_MCP_ENABLE_PRUNE", False):
        capabilities.add(Capability.PRUNE_DOCUMENTATION)
    return Principal("mcp-service-account", "mcp", frozenset(capabilities))
