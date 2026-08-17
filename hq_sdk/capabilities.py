"""Command, identity, and capability primitives shared by every adapter."""

from pydantic import BaseModel, ConfigDict

from application.capabilities import (
    CapabilitySpec,
    capability_registry,
    describe_capabilities,
    execute_capability,
)
from application.security import (
    AuthorizationError,
    Capability,
    Principal,
    cli_principal,
    mcp_principal,
    web_principal,
)


class StrictCommand(BaseModel):
    """Base for JSON commands that reject misspelled or obsolete fields."""

    model_config = ConfigDict(extra="forbid")


__all__ = [
    "AuthorizationError",
    "Capability",
    "CapabilitySpec",
    "Principal",
    "StrictCommand",
    "capability_registry",
    "cli_principal",
    "describe_capabilities",
    "execute_capability",
    "mcp_principal",
    "web_principal",
]
