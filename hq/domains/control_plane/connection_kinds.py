"""What each kind of connection is called, and how its credential can be held."""

from collections.abc import Mapping

from hq.platform.application.connection_contracts import UNKNOWN_CREDENTIAL

from .provider_adapters import CONNECTIONS
from .provider_spec import ConnectionKind, ConnectionShape

# Every connection provider, declared by the provider module that uses it and
# gathered from the admitted set, so a provider cannot be admitted without one.
CONNECTION_KINDS: Mapping[str, ConnectionKind] = frozendict(CONNECTIONS)


def _shapes() -> Mapping[str, ConnectionShape]:
    found: dict[str, ConnectionShape] = {}
    for kind in CONNECTION_KINDS.values():
        if found.setdefault(kind.shape.name, kind.shape) is not kind.shape:
            raise ValueError(f"Two connection shapes are named {kind.shape.name!r}.")
    return frozendict(dict(sorted(found.items())))


# Every shape a connection arrives in, by name.
SHAPES = _shapes()

# The two facts most callers want, derived so neither can name a provider the
# other does not.
CONNECTION_CREDENTIALS: Mapping[str, str] = frozendict(
    {provider: kind.credential for provider, kind in CONNECTION_KINDS.items()}
)
CONNECTION_LABELS: Mapping[str, str] = frozendict({provider: kind.label for provider, kind in CONNECTION_KINDS.items()})


def connection_credential(provider: str) -> str:
    """The credential model of one connection provider; unknown for a provider
    HQ does not model, whose credential it still holds."""

    return CONNECTION_CREDENTIALS.get(provider, UNKNOWN_CREDENTIAL)
