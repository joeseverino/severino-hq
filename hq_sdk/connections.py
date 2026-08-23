"""Safe, discoverable connection contracts shared by HQ and plugins."""

from application.connections import (
    ConnectionAbility,
    ConnectionFact,
    ConnectionInstance,
    ConnectionLink,
    ConnectionSpec,
    describe_connections,
    list_connections,
)

__all__ = [
    "ConnectionAbility",
    "ConnectionFact",
    "ConnectionInstance",
    "ConnectionLink",
    "ConnectionSpec",
    "describe_connections",
    "list_connections",
]
