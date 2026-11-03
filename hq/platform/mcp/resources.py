"""What a person can attach from HQ in an agent's client, as MCP resources.

A few fixed views, and one template for each kind of record the caller may
read one of, so an extension's kinds appear here as the registry holds them.
Each reads through the application's own read and its authorization, as the
caller, exactly as ``list_resource`` and ``get_resource`` do; docs/API.md says
the URIs.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from mcp import types
from mcp.shared.exceptions import MCPError

from hq.platform.application import approvals, resources as registry
from hq.platform.application.resources import ResourceCollection
from hq.platform.application.security import Principal

SCHEME = "hq"
MIME = "application/json"


@dataclass(frozen=True, slots=True)
class View:
    name: str
    title: str
    description: str
    read: Callable[[Principal], Mapping[str, Any]]

    @property
    def uri(self) -> str:
        return f"{SCHEME}://{self.name}"


def action_queue(principal: Principal) -> ResourceCollection:
    """The queue as its kind's list_fields keep each item: what a person reads it by, and its page in HQ."""

    return registry.list_resource("action.items", None, principal=principal)


def hq_itself(principal: Principal) -> ResourceCollection:
    return registry.list_resource("hq", None, principal=principal)


def held_requests(principal: Principal) -> dict[str, Any]:
    """The changes waiting for a person that ``principal`` may read: an agent's own, or every one for a person."""

    items = [serialize_held(held) for held in approvals.pending(principal=principal)]
    return {"items": items, "count": len(items)}


def serialize_held(held: Any) -> dict[str, Any]:
    return {"asks": str(approvals.asked(held)), **approvals.serialize_approval(held)}


VIEWS = {
    view.name: view
    for view in (
        View("action.items", "Needs you", "HQ's action queue: what needs doing, from every domain.", action_queue),
        View(
            "approvals",
            "Held requests",
            "The changes you asked for that wait for a person; a person reads every one.",
            held_requests,
        ),
        View(
            "hq",
            "HQ",
            (
                "HQ itself: its release, where it runs, its jobs and its open problems. "
                "system_health says whether it answers; the status-brief prompt is a brief of both."
            ),
            hq_itself,
        ),
    )
}


def listed() -> types.ListResourcesResult:
    return types.ListResourcesResult(
        resources=[
            types.Resource(uri=view.uri, name=view.name, title=view.title, description=view.description, mime_type=MIME)
            for view in VIEWS.values()
        ]
    )


def templates(principal: Principal) -> types.ListResourceTemplatesResult:
    """A template for each kind with records to read one at a time that the caller may read."""

    return types.ListResourceTemplatesResult(
        resource_templates=[
            types.ResourceTemplate(
                uri_template=f"{SCHEME}://{name}/{{{spec.identifier}}}",
                name=name,
                title=spec.label,
                description=f"{spec.summary} One record, by its {spec.identifier}.",
                mime_type=MIME,
            )
            for name, spec in registry.resource_registry().items()
            if spec.detail_handler and spec.identifier and registry.readable(spec, principal)
        ]
    )


# The most identifiers one completion offers: the spec's cap on a completion's
# values, and the largest page a listing serves, so one read answers it.
COMPLETIONS = 100


def completed(uri_template: str, argument: str, typed: str, principal: Principal) -> types.Completion:
    """The identifiers a template's record may be named by that start with
    ``typed``, read from its kind's listing as ``principal``, as ``list_resource``
    reads it; nothing for a kind the caller may not read or cannot list."""

    spec = registry.resource_registry().get(urlsplit(uri_template).netloc)
    if (
        spec is None
        or argument != spec.identifier
        or not (spec.detail_handler and spec.list_handler and spec.list_query_type)
        or not registry.readable(spec, principal)
    ):
        return types.Completion(values=[])
    query = {"limit": COMPLETIONS} if "limit" in spec.list_query_type.model_fields else None
    page = registry.list_resource(spec.name, query, principal=principal, fields=[spec.identifier])
    found = [str(item[argument]) for item in page["items"] if argument in item]
    matches = [value for value in found if value.startswith(typed)]
    return types.Completion(
        values=matches[:COMPLETIONS], has_more=len(matches) > COMPLETIONS or bool(page["truncated"])
    )


def _nothing_at(uri: str) -> MCPError:
    return MCPError(
        types.INVALID_PARAMS,
        f"HQ has no resource at {uri}. resources/list names the fixed views, and "
        "resources/templates/list the hq://<kind>/{identifier} records you may read.",
        data={"next": {"method": "resources/templates/list"}},
    )


def read(uri: str, principal: Principal) -> Mapping[str, Any]:
    """The record or view a URI names, read as ``principal``.

    ``hq://<view>`` is a fixed view; ``hq://<kind>/<identifier>`` is one
    record, its identifier percent-encoded as a template expands it.
    """

    parts = urlsplit(uri)
    if parts.scheme != SCHEME or parts.query or parts.fragment:
        raise _nothing_at(uri)
    identifier = unquote(parts.path.removeprefix("/"))
    if not identifier:
        view = VIEWS.get(parts.netloc)
        if view is None:
            raise _nothing_at(uri)
        return view.read(principal)
    # Text, as an address carries it: the kind's own identifier type parses it.
    return registry.get_resource(parts.netloc, identifier, principal=principal, strict=False)
