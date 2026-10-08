"""What each kind of connection is called, and how its credential can be held."""

from collections.abc import Mapping
from types import MappingProxyType

from .provider_adapters import CONNECTIONS
from .provider_spec import ConnectionKind

# Every connection provider, declared by the provider module that uses it and
# gathered from the admitted set, so a provider cannot be admitted without one.
CONNECTION_KINDS: Mapping[str, ConnectionKind] = MappingProxyType(CONNECTIONS)

# The two facts most callers want, derived so neither can name a provider the
# other does not.
CONNECTION_CREDENTIALS: Mapping[str, str] = MappingProxyType(
    {provider: kind.credential for provider, kind in CONNECTION_KINDS.items()}
)
CONNECTION_LABELS: Mapping[str, str] = MappingProxyType(
    {provider: kind.label for provider, kind in CONNECTION_KINDS.items()}
)


def connection_credential(provider: str) -> str:
    """The credential model of one connection provider; blank when unnamed."""

    return CONNECTION_CREDENTIALS.get(provider, "")
