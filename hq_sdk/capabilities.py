"""For what an extension can do: declare its capabilities and strict commands, and run a write."""

from pydantic import BaseModel, ConfigDict

from hq.platform.application.capabilities import (
    CapabilitySpec,
    capability_registry,
    describe_capabilities,
    execute_capability,
)
from hq.platform.application.command_fields import Choices, Describe, Names, Refers
from hq.platform.application.integration_specs import ON_ITS_PAGE, CommandAbout, CommandFact
from hq.platform.application.record_errors import InputError
from hq.platform.application.security import (
    AuthorizationError,
    Capability,
    Principal,
    cli_principal,
    mcp_principal,
    web_principal,
)
from hq.platform.application.web_writes import execute_posted, refusal_response, refused


class StrictCommand(BaseModel):
    """Base for JSON commands that reject misspelled or obsolete fields."""

    model_config = ConfigDict(extra="forbid")


__all__ = [
    "ON_ITS_PAGE",
    "AuthorizationError",
    "Capability",
    "CapabilitySpec",
    "Choices",
    "CommandAbout",
    "CommandFact",
    "Describe",
    "InputError",
    "Names",
    "Principal",
    "Refers",
    "StrictCommand",
    "capability_registry",
    "cli_principal",
    "describe_capabilities",
    "execute_capability",
    "execute_posted",
    "mcp_principal",
    "refusal_response",
    "refused",
    "web_principal",
]
