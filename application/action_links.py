"""Safe links from derived state to canonical HQ workflows.

An action link does not execute anything. It explains which existing use case
owns the next step and carries enough machine-readable identity for web, API,
MCP, and topology to agree about it. The destination remains responsible for
authorization and mutation; capability links are additionally filtered here so
a projection never advertises authority its principal does not hold.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from django.urls import NoReverseMatch, reverse

from .contracts import route_url
from .security import AuthorizationError, Principal

if TYPE_CHECKING:
    from .connections import ConnectionSpec


@dataclass(frozen=True)
class ActionLink:
    """One safe route from observed state to an existing HQ use case."""

    name: str
    label: str
    effect: str
    url: str
    method: str = "GET"
    capability: str = ""
    target: str = ""
    reason: str = ""
    recommended: bool = False


_CONNECTION_ROUTES = (
    ("open", "Open connections", "web_route"),
    ("manage", "Manage", "management_route"),
    ("set_up", "Set up", "setup_route"),
)


def connection_action_links(spec: ConnectionSpec) -> tuple[ActionLink, ...]:
    """Resolve only the destinations a connection family declared."""

    actions = []
    seen: set[str] = set()
    for name, label, field in _CONNECTION_ROUTES:
        url = route_url(getattr(spec, field))
        if not url or url in seen:
            continue
        seen.add(url)
        actions.append(ActionLink(name, label, "read", url))
    if spec.documentation_url:
        actions.append(
            ActionLink("documentation", "Documentation", "read", spec.documentation_url)
        )
    return tuple(actions)


def capability_action_link(
    name: str,
    effect: str,
    label: str,
    *,
    principal: Principal,
) -> ActionLink | None:
    """Link to a command only when its canonical contract permits ``principal``."""

    if not name:
        return None
    # Imported here because capabilities compose plugin specs, which themselves
    # may declare connections. Keeping the dependency at call time avoids a
    # registry import cycle while retaining one authorization implementation.
    from .capabilities import authorize_capability, capability_registry

    spec = capability_registry().get(name)
    if spec is None:
        return None
    try:
        authorize_capability(spec, principal)
    except AuthorizationError:
        return None
    try:
        command_url = reverse("command", kwargs={"name": name})
    except NoReverseMatch:
        return None
    return ActionLink(
        "command",
        label,
        effect,
        command_url,
        capability=name,
        reason="Required connection scopes and HQ authority are both confirmed.",
    )


def connection_relationship_link(spec_name: str, instance_id: str) -> ActionLink | None:
    """Focus the one derived topology node for a connection instance."""

    topology = route_url("control_plane:topology")
    if not topology:
        return None
    node_id = f"connection:{spec_name}:{instance_id}"
    return ActionLink(
        "relationships",
        "Show relationships",
        "read",
        f"{topology}?{urlencode({'focus': node_id})}#map",
        reason="Derived targets, dependencies, abilities, and governed resources.",
    )


def recommend_connection_action(
    actions: tuple[ActionLink, ...],
    *,
    unhealthy: bool,
    missing_scope_count: int,
    unknown_scope_count: int,
) -> tuple[ActionLink, ...]:
    """Mark a truthful next move without inventing provider behavior."""

    preferred = next((item for item in actions if item.name == "manage"), None)
    if preferred is None:
        preferred = next((item for item in actions if item.name == "set_up"), None)
    if preferred is None:
        return actions

    if missing_scope_count:
        label = "Review access"
        reason = (
            f"{missing_scope_count} required provider "
            f"scope{'s are' if missing_scope_count != 1 else ' is'} missing."
        )
    elif unhealthy:
        label = "Inspect issue"
        reason = "The latest cached connection observation needs attention."
    elif unknown_scope_count:
        label = "Verify access"
        reason = "The provider did not report enough scope evidence to decide."
    else:
        return actions

    return tuple(
        replace(item, label=label, reason=reason, recommended=True)
        if item is preferred
        else item
        for item in actions
    )
