"""Safe links from derived state to canonical HQ workflows.

An action link does not execute anything. It explains which existing use case
owns the next step and carries enough machine-readable identity for web, API,
MCP, and topology to agree about it. The destination remains responsible for
authorization and mutation; capability links are additionally filtered here so
a projection never advertises authority its principal does not hold.
"""

from collections.abc import Iterable, Mapping
from dataclasses import MISSING, dataclass, fields
from typing import Any, Protocol

from django.urls import NoReverseMatch

from hq.platform.application.routes import reverse

from .contracts import Effect, route_url
from .references import Kind
from .security import Principal
from .ui import counted
from .url_query import with_query
from .workflow_contracts import ActionLink
lazy from . import capabilities, outbound_work
lazy from .pages import command_reach


class ConnectionLinkSpec(Protocol):
    """The route fields needed to render connection actions without a registry import."""

    web_route: str
    management_route: str
    setup_route: str
    documentation_url: str


_CONNECTION_ROUTES = (
    ("open", "Open connections", "web_route"),
    ("manage", "Manage", "management_route"),
    ("set_up", "Set up", "setup_route"),
)


def connection_action_links(spec: ConnectionLinkSpec) -> tuple[ActionLink, ...]:
    """Resolve only the destinations a connection family declared."""

    actions = []
    seen: set[str] = set()
    for name, label, field in _CONNECTION_ROUTES:
        url = route_url(getattr(spec, field))
        if not url or url in seen:
            continue
        seen.add(url)
        actions.append(read_link(name, label, url))
    if spec.documentation_url:
        actions.append(read_link("documentation", "Documentation", spec.documentation_url))
    return tuple(actions)


def read_link(name: str, label: str, url: str, *, reason: str = "", recommended: bool = False) -> ActionLink:
    """A link that only opens a page: a read, by GET, that runs no command.

    The way every offer that is no command's is built, so only
    ``command_action`` gives one an effect, a method or a capability.
    """

    return _built(name=name, label=label, effect=Effect.READ, url=url, reason=reason, recommended=recommended)


def open_link(name: str, url: str) -> tuple[ActionLink, ...]:
    """The page of the thing called ``name``, said by that name wherever the
    link stands apart from it; none without a page."""

    return (read_link("open", f"Open {name}", url),) if url else ()


def _built(**values: Any) -> ActionLink:
    """The link whose fields the page or command factory has derived."""

    link = object.__new__(ActionLink)
    for field in fields(ActionLink):
        value = values.get(field.name, field.default)
        if value is MISSING:
            raise TypeError(f"Missing link field: {field.name}")
        object.__setattr__(link, field.name, value)
    return link


# The route of every command's own page, by its name.
COMMAND_ROUTE = "command"


def command_url(name: str, target: str = "", *, kinds: Iterable[str] = (), **query: str | tuple[str, ...]) -> str:
    """The one way to link to a command's form: its page, its target, encoded.

    So a target is encoded as the form reads it back, and an unmounted command
    yields "" rather than a dead link. An architecture test holds every other
    module to it.
    """

    try:
        url = reverse(COMMAND_ROUTE, kwargs={"name": name})
    except NoReverseMatch:
        return ""
    params = [("target", target)] if target else []
    params += [("kind", kind) for kind in kinds]
    params += [
        (key, each)
        for key, value in query.items()
        for each in (value if isinstance(value, tuple) else (value,))
        if each
    ]
    return with_query(url, params)


def command_action(
    name: str,
    capability: str,
    url: str = "",
    *,
    target: str | Mapping[str, str] = "",
    method: str = "",
    query: Mapping[str, str] | None = None,
    variant: str = "",
    intent: str = "",
    reason: str = "",
    recommended: bool = False,
) -> ActionLink:
    """A link that runs a command, in the command's own words.

    The one place an ``ActionLink`` names a capability (its constructor
    refuses every caller), so its label, effect, address and method come from the
    declaration and cannot drift from it.
    """

    spec = capabilities.command_spec(capability)
    subject = "" if isinstance(target, Mapping) else target
    if not url:
        url, answers_get = command_reach(capability, target, variant)
        method = "GET" if answers_get else "POST"
        if url and query:
            url = with_query(url, query)
    return _built(
        name=name,
        # ``variant`` picks one of the words the command declares for its
        # modes; ``intent`` is a remedy's own words for what this press does
        # ("Add an access list"), where the command's would not.
        label=intent or spec.words(variant),
        effect=spec.effect,
        url=url,
        method=method or "GET",
        capability=capability,
        target=subject,
        reason=reason,
        recommended=recommended,
        # Why the command would be refused for ``target`` now, so every
        # renderer draws it unusable with that reason instead of offering a
        # press that fails.
        refusal=outbound_work.refusal(capability, subject),
    )


def capability_action_link(
    name: str,
    *,
    principal: Principal,
    variant: str = "",
    subject: str = "",
    subject_resource: str | None = None,
) -> ActionLink | None:
    """Link to a command only when its canonical contract permits ``principal``.

    ``subject`` is the key of the record the link is offered beside, a record
    of ``subject_resource`` (None for one no resource lists, as a connection
    is). It is the target of a command about a record of that same resource.
    A command about another resource's records is linked to its form, which
    chooses one, since this key names none of them. One about no record
    ignores it.
    """

    spec = _permitted_spec(name, principal)
    if spec is None:
        return None
    target = subject if spec.target_kind and spec.subject_resource == subject_resource else ""
    link = command_action("command", name, target=target, variant=variant, reason="Its permissions were checked.")
    return link if link.url else None


def _permitted_spec(name: str, principal: Principal):
    """The capability's spec when ``principal`` may run it, else None."""

    if not name:
        return None
    spec = capabilities.capability_spec(name)
    if spec is None:
        return None
    return spec if spec.permitted_to(principal) else None


READ_NOW_CAPABILITY = "infrastructure.controller.refresh"


def read_now_link(
    principal: Principal,
    *,
    connection_ref: str = "",
    kind: str = "",
    every_connection: bool = False,
) -> ActionLink | None:
    """A POST asking the controller to read a connection, a kind or everything now.

    None when ``principal`` may not ask. The subject rides in the URL so every
    renderer posts the same request. Asked of everything, it says the
    command's "all" words.
    """

    spec = _permitted_spec(READ_NOW_CAPABILITY, principal)
    destination = route_url("control_plane:read_now")
    if spec is None or not destination:
        return None
    params = {
        key: value
        for key, value in (
            ("connection_ref", connection_ref),
            ("kind", kind),
            ("every_connection", "1" if every_connection else ""),
        )
        if value
    }
    return command_action(
        "read_now",
        READ_NOW_CAPABILITY,
        with_query(destination, params),
        method="POST",
        target=connection_ref or kind,
        variant="all" if every_connection else "",
        reason="Reads it again now.",
    )


def read_now_payload(values) -> dict[str, object]:
    """The command ``read_now_link``'s URL, or a form, asks for: the inverse."""

    payload: dict[str, object] = {
        key: str(values.get(key, "")).strip() for key in ("connection_ref", "kind") if str(values.get(key, "")).strip()
    }
    if values.get("every_connection") == "1":
        payload["every_connection"] = True
    return payload


def topology_url(
    node_id: str,
    *,
    direction: str = "",
    depth: int | None = None,
    lens: str = "",
    fragment: str = "trace",
) -> str:
    """Address one topology investigation from every delivery surface."""

    topology = route_url("control_plane:topology")
    if not topology or not node_id:
        return ""
    params: dict[str, str | int] = {"focus": node_id}
    if direction:
        params["direction"] = direction
    if depth is not None:
        params["depth"] = depth
    if lens:
        params["lens"] = lens
    return with_query(topology, params, fragment=fragment)


@dataclass(frozen=True, slots=True)
class Investigation:
    """One way to look around a node: the trace a page opens and a topology read is narrowed by."""

    name: str
    label: str
    reason: str
    direction: str = ""
    depth: int | None = None


# Declared once, so the link a page follows and the read an agent calls are one trace.
INVESTIGATIONS = {
    investigation.name: investigation
    for investigation in (
        Investigation("topology", "Map neighborhood", "What this is connected to."),
        Investigation(
            "impact",
            "Impact",
            "Everything that stops working if this does.",
            direction="outbound",
            depth=3,
        ),
    )
}


def topology_investigation_links(node_id: str) -> tuple[ActionLink, ...]:
    """The canonical read-only ways to understand an affected topology node."""

    links = tuple(
        read_link(
            investigation.name,
            investigation.label,
            topology_url(node_id, direction=investigation.direction, depth=investigation.depth),
            reason=investigation.reason,
        )
        for investigation in INVESTIGATIONS.values()
    )
    return links if all(link.url for link in links) else ()


def action_with_return(action: ActionLink, route_name: str) -> ActionLink:
    """Keep a command inside the workflow that discovered it."""

    destination = route_url(route_name)
    if not destination or not action.url:
        return action
    return _built(
        **{field.name: getattr(action, field.name) for field in fields(action) if field.name != "url"},
        url=with_query(action.url, {"next": destination}),
    )


def connection_relationship_link(spec_name: str, instance_id: str) -> ActionLink | None:
    """Focus the one derived topology node for a connection instance."""

    url = topology_url(
        Kind.CONNECTION.of(f"{spec_name}:{instance_id}"),
        fragment="map",
    )
    if not url:
        return None
    return read_link("relationships", "Show links", url, reason="What it reads, reaches and changes.")


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
        reason = f"Missing {counted(missing_scope_count, 'permission')} it needs."
    elif unhealthy:
        label = "See the problem"
        reason = "The last read through this connection failed."
    elif unknown_scope_count:
        label = "Verify access"
        reason = "The service did not say what this token may do."
    else:
        return actions

    return tuple(
        _built(
            **{
                field.name: getattr(item, field.name)
                for field in fields(item)
                if field.name not in {"label", "reason", "recommended"}
            },
            label=label,
            reason=reason,
            recommended=True,
        )
        if item is preferred
        else item
        for item in actions
    )
