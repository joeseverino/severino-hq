"""Dependency-free contracts a domain uses to emit its own connection.

Here rather than in ``connections`` because a domain gateway declares its
connection, and ``connections`` reads back into the domains to compose the
registry: sharing the reading module would close an import cycle. These are
plain records, so a declaration costs the declaring module nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .security import Capability


@dataclass(frozen=True)
class ConnectionAbility:
    """One thing a connection permits HQ to do, without credential material."""

    name: str
    label: str
    summary: str
    effect: str = "read"
    required_scopes: tuple[str, ...] = ()
    capability: str = ""
    # Resource kinds this ability governs. The relation is explicit because an
    # ability name describes what a connection can do; it is not inherently a
    # ManagedResource kind, and separate connection families may reuse it.
    governs_kinds: tuple[str, ...] = ()
    # The canonical resource catalog these governed kinds belong to. Commands
    # against that resource can then be discovered from this ability without a
    # provider-specific command list.
    subject_resource: str = ""


@dataclass(frozen=True)
class ConnectionLink:
    """A safe relationship from a connection to something HQ can name."""

    label: str
    url: str = ""
    # Explicit identity for a ManagedResource dependency. A rendered label is
    # presentation and must never become a join key by coincidence.
    resource_key: str = ""


@dataclass(frozen=True)
class ConnectionFact:
    """A small provider-owned fact that is useful in a generic connection row."""

    label: str
    value: str


@dataclass(frozen=True)
class ConnectionInstance:
    """One configured connection as its owning domain last observed it."""

    id: str
    label: str
    kind: str
    status: str
    status_label: str
    detail: str = ""
    endpoint: str = ""
    observed_at: datetime | None = None
    granted_scopes: tuple[str, ...] = ()
    scopes_known: bool = False
    ability_names: tuple[str, ...] = ()
    targets: tuple[ConnectionLink, ...] = ()
    dependencies: tuple[ConnectionLink, ...] = ()
    facts: tuple[ConnectionFact, ...] = ()
    # The observer that supplied this reading. Optional because an extension
    # may read an account directly rather than through an infrastructure
    # controller; when present it gives topology a real edge instead of asking
    # a rendered label to carry identity.
    controller_id: str = ""


@dataclass(frozen=True)
class ConnectionSpec:
    """One declaration of a connection family and its cached instance provider."""

    name: str
    label: str
    summary: str
    required_capability: Capability | str | tuple[Capability | str, ...]
    instance_provider: Callable[[], tuple[ConnectionInstance, ...]]
    abilities: tuple[ConnectionAbility, ...] = ()
    web_route: str = "control_plane:connections"
    management_route: str = ""
    setup_route: str = ""
    documentation_url: str = ""
    secret_store: str = ""
    # What this family's emptiness means, in its own words. The default suits a
    # family that emits from configuration; one fed by controller reports
    # overrides it to say so.
    empty_message: str = (
        "No credential for this is configured in this environment. "
        "Connections appear once one is present."
    )

    @property
    def required_capabilities(self) -> tuple[Capability | str, ...]:
        if isinstance(self.required_capability, tuple):
            return self.required_capability
        return (self.required_capability,)
