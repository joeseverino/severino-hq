"""Command, identity, and capability primitives shared by every adapter."""

from pydantic import BaseModel, ConfigDict

from hq.platform.application.capabilities import (
    CapabilitySpec,
    capability_registry,
    describe_capabilities,
    execute_capability,
)
from hq.platform.application.command_fields import Choices, Describe, Names, Refers
from hq.platform.application.integration_specs import ON_ITS_PAGE
from hq.platform.application.security import (
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
    "ON_ITS_PAGE",
    "AuthorizationError",
    "Capability",
    "CapabilitySpec",
    "Choices",
    "Describe",
    "Names",
    "Principal",
    "Refers",
    "StrictCommand",
    "capability_registry",
    "cli_principal",
    "describe_capabilities",
    "execute_capability",
    "mcp_principal",
    "web_principal",
]
