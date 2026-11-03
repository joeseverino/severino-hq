"""The principals a topology test reads as."""

from ..security import Capability, Principal

READ = Principal("reader", "test", frozenset({Capability.READ}))
MANAGE = Principal(
    "operator",
    "test",
    frozenset(
        {
            Capability.READ,
            Capability.MANAGE_INFRASTRUCTURE,
            Capability.REQUEST_CERTIFICATE_RENEWAL,
        }
    ),
)
NONE = Principal("nobody", "test", frozenset())
