"""The operator a finding test reads as."""

from ..security import Capability, Principal

MANAGE = Principal(
    "operator",
    "test",
    frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}),
)
