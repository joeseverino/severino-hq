"""For what an extension can do: declare its capabilities and their commands, and run a write.

A command is a frozen dataclass or a TypedDict: HQ closes it to the fields it names.
A field's rule is ``Checked`` with a ``Reason``, the one way a value is refused.
A field that is a file, or a list's items that are, carries ``Upload``.
"""

from hq.platform.application.capabilities import (
    CapabilitySpec,
    command_spec,
    describe_capabilities,
    execute_capability,
)
from hq.platform.application.command_fields import (
    Checked,
    Choices,
    Describe,
    Names,
    NonEmpty,
    Reason,
    Refers,
    RequiredText,
    Upload,
    checked,
)
from hq.platform.application.contracts import Effect
from hq.platform.application.integration_specs import Kept, keeps
from hq.platform.application.record_errors import InputError, RecordNotFound
from hq.platform.application.record_saves import as_sent, written
from hq.platform.application.records import KEPT
from hq.platform.application.security import (
    AuthorizationError,
    Capability,
    Principal,
    cli_principal,
    mcp_principal,
    web_principal,
)
from hq.platform.application.web_writes import execute_posted
from hq.platform.core.web_refusals import refusal_response, refused

__all__ = [
    "KEPT",
    "AuthorizationError",
    "Capability",
    "CapabilitySpec",
    "Checked",
    "Choices",
    "Describe",
    "Effect",
    "InputError",
    "Kept",
    "Names",
    "NonEmpty",
    "Principal",
    "Reason",
    "RecordNotFound",
    "Refers",
    "RequiredText",
    "Upload",
    "as_sent",
    "checked",
    "cli_principal",
    "command_spec",
    "describe_capabilities",
    "execute_capability",
    "execute_posted",
    "keeps",
    "mcp_principal",
    "refusal_response",
    "refused",
    "web_principal",
    "written",
]
