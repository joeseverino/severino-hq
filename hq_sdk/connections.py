"""For a credential an extension holds: declare the connection and what it may see and do."""

from hq.platform.application.connections import (
    ConnectionAbility,
    ConnectionFact,
    ConnectionInstance,
    ConnectionLink,
    ConnectionSpec,
    describe_connections,
)

__all__ = [
    "ConnectionAbility",
    "ConnectionFact",
    "ConnectionInstance",
    "ConnectionLink",
    "ConnectionSpec",
    "describe_connections",
]
