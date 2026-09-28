"""The connections this controller holds, as its rendered environment declares them."""

from __future__ import annotations

import os
from typing import Any

from control_plane.provider_adapters.contracts import ProviderError
from . import provider_http


def connection_prefixes() -> dict[str, str]:
    """Every connection the environment carries, as ref -> env prefix.

    The rendered environment is the inventory. `render-controller-env.sh`
    resolves each 1Password item into `<PREFIX>_CONNECTION_REF` alongside that
    connection's values, so what the controller can reach is exactly what it was
    given credentials for: nothing here has to be told separately.
    """

    suffix = "_CONNECTION_REF"
    return {
        value: name[: -len(suffix)]
        for name, value in os.environ.items()
        if name.endswith(suffix) and value
    }


def connection_provider(connection_ref: str) -> str:
    """What kind of thing one connection reaches.

    The env prefix is the answer unless the 1Password item says otherwise, which
    makes the long-standing convention (`ADGUARD_URL` is AdGuard's URL) the
    rule rather than a coincidence every provider had to restate. Declaring it
    on the item is what lets two of the same kind coexist: `PORTAINER_HOME` and
    `PORTAINER_CLOUD` are both portainer, and neither has to be named here.
    """

    prefix = connection_prefixes().get(connection_ref, "")
    if not prefix:
        return ""
    return os.environ.get(f"{prefix}_PROVIDER", "").strip() or prefix.lower()


def effective_provider(connection_ref: str, ssh_refs: set[str] | None = None) -> str:
    """The provider a connection acts as: its own, or ``ssh`` for a transport.

    A connection whose provider has no probe of its own but carries a host and
    a user is an SSH transport, whatever its prefix spells.
    """

    from .providers import _CONNECTION_PROBES

    provider = connection_provider(connection_ref)
    if provider in _CONNECTION_PROBES:
        return provider
    refs = set(ssh_connection_refs()) if ssh_refs is None else ssh_refs
    if connection_ref in refs:
        prefix = connection_prefixes().get(connection_ref, "")
        return os.environ.get(f"{prefix}_PROVIDER", "").strip() or "ssh"
    return provider


def connection_prefix(provider: str, connection_ref: str = "") -> str:
    """The env prefix for one connection: the one named, or the only one.

    A provider that takes a ``connection_ref`` resolves it here and reaches
    exactly that endpoint, so a second Portainer is a second 1Password item and
    nothing more. A provider whose spec names none gets the sole connection for
    its kind; two of them is an error rather than a silent choice between them,
    because guessing would reconcile the wrong estate.

    Falls back to the provider's own name in upper case, which is the prefix a
    deployment that has not yet labelled its connections is already using.
    """

    inventory = connection_prefixes()
    if connection_ref:
        prefix = inventory.get(connection_ref)
        if not prefix:
            raise ProviderError(
                f"No connection named {connection_ref!r} was supplied to the "
                "controller."
            )
        return prefix
    candidates = sorted(
        prefix
        for ref, prefix in inventory.items()
        if connection_provider(ref) == provider
    )
    if len(candidates) > 1:
        raise ProviderError(
            f"More than one connection is a {provider}; the resource has to say which."
        )
    return candidates[0] if candidates else provider.upper()


def provider_connection_refs(provider: str) -> tuple[str, ...]:
    """Every connection that is one of these, as its own item declares.

    What a sweep iterates. Two Portainers are two 1Password items and get swept
    as two, so an estate grows by being given a credential rather than by being
    named anywhere. Falls back to the conventional prefix for a deployment whose
    items do not carry the field yet, which is one connection, the one it has.
    """

    declared = tuple(
        sorted(
            ref for ref in connection_prefixes() if connection_provider(ref) == provider
        )
    )
    if declared:
        return declared
    conventional = os.environ.get(f"{provider.upper()}_CONNECTION_REF", "").strip()
    return (conventional,) if conventional else ()


def connection_store(prefix: str) -> dict[str, str]:
    """Where the connection's credential is kept, as the renderer named it.

    The vault and item id the item was rendered from, and the reference to the
    bootstrap credential that may mint a replacement when the item names one.
    References only; the bootstrap credential itself is never rendered here.
    """

    found = {
        key: os.environ.get(f"{prefix}_{name}", "").strip()
        for key, name in (
            ("vault", "STORE_VAULT"),
            ("item", "STORE_ITEM"),
            ("bootstrap", "BOOTSTRAP"),
        )
    }
    return {key: value for key, value in found.items() if value}


def connection_role(connection_ref: str) -> str:
    """What a connection is used for, where the item says so.

    Separate from ``connection_provider`` on purpose. A provider says what kind
    of system answers, and the connections page keys a machine's abilities off
    it; a role says what HQ reaches this one *for*. An SSH host that serves
    Caddy and one that is shared hosting are the same kind of system and the
    same kind of credential, and only the role tells them apart.
    """

    prefix = connection_prefixes().get(connection_ref, "")
    if not prefix:
        return ""
    return os.environ.get(f"{prefix}_ROLE", "").strip()


def connection_manages(connection_ref: str) -> bool:
    """Whether the connection's item says HQ manages through it.

    Declared as ``<PREFIX>_MANAGES``. Absent or anything but a yes means the
    connection only observes: a credential's permissions are not readable
    without more permissions, so the item states the intent.
    """

    prefix = connection_prefixes().get(connection_ref, "")
    if not prefix:
        return False
    value = os.environ.get(f"{prefix}_MANAGES", "").strip().lower()
    return value in {"1", "true", "yes"}


def connection_refs_for_role(role: str) -> tuple[str, ...]:
    """Every SSH connection declared for ``role``.

    Empty when nothing declares one, which callers read as "nobody has said",
    not as "none of them". A discovery that asked only declared hosts before
    any host was declared would find nothing and report it as an empty estate.
    """

    return tuple(
        ref for ref in ssh_connection_refs() if connection_role(ref) == role
    )


def ssh_connection_refs() -> tuple[str, ...]:
    """Connections rendered through the ssh_transport projection.

    Identified by the values that projection produces rather than by a declared
    kind: a connection carrying a host and a user is one this can open.
    """

    return tuple(
        sorted(
            ref
            for ref, prefix in connection_prefixes().items()
            if os.environ.get(f"{prefix}_HOST") and os.environ.get(f"{prefix}_USER")
        )
    )


def ssh_target(connection_ref: str) -> dict[str, Any]:
    """The endpoint for an SSH connection, from the rendered environment."""

    prefix = connection_prefixes().get(connection_ref)
    if not prefix or not os.environ.get(f"{prefix}_HOST"):
        raise ProviderError(f"Unknown certificate transport: {connection_ref}.")
    port = provider_http.required(prefix, "PORT")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ProviderError(f"The port configured for {connection_ref} is not a port number.")
    return {
        "host": provider_http.required(prefix, "HOST"),
        "port": int(port),
        "user": provider_http.required(prefix, "USER"),
        "host_key": provider_http.required(prefix, "HOST_KEY"),
    }
