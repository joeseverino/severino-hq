"""For what an extension can do: declare its capabilities and their commands, and run a write.

A command is a frozen dataclass or a TypedDict: HQ closes it to the fields it names.
"""

from hq.platform.application.capabilities import (
    CapabilitySpec,
    capability_registry,
    describe_capabilities,
    execute_capability,
)
from hq.platform.application.command_fields import Choices, Describe, Names, Refers
from hq.platform.application.contracts import Effect
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

__all__ = [
    "ON_ITS_PAGE",
    "AuthorizationError",
    "Capability",
    "CapabilitySpec",
    "Choices",
    "CommandAbout",
    "CommandFact",
    "Describe",
    "Effect",
    "InputError",
    "Names",
    "Principal",
    "Refers",
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
