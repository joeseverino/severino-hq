"""The manager a topology test reads as, who may also renew a certificate."""

from ..security import Capability, Principal

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
