"""What a person can attach from HQ in an agent's client, as MCP resources.

A few fixed views, and one template for each kind of record the caller may
read one of, so an extension's kinds appear here as the registry holds them.
Each reads through the application's own read and its authorization, as the
caller, exactly as ``list_resource`` and ``get_resource`` do; docs/API.md says
the URIs.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from typing import Any, cast

from mcp import types
from mcp.shared.exceptions import MCPError
from mcp.shared.uri_template import UriTemplate

from hq.platform.application import approval_review, approvals, resources as registry
from hq.platform.application.access import Access, holding
from hq.platform.application.integration_specs import PAGE_FIELD
from hq.platform.application.projection import complete
from hq.platform.application.resources import ResourceCollection, ResourceSpec
from hq.platform.application.security import Capability, Principal

SCHEME = "hq"
MIME = "application/json"

# A fixed view's address. Every address is the SDK's RFC 6570 template, so the
# client's expansion and HQ's match encode and decode a value the same way.
VIEW = UriTemplate.parse(f"{SCHEME}://{{view}}")

# Who reads the action queue, the held requests and HQ itself: each read requires it.
READS = holding(Capability.READ)


@cache
def record_template(kind: str, identifier: str) -> UriTemplate:
    """The address of one record of ``kind``, named by its ``identifier`` field."""

    return UriTemplate.parse(f"{SCHEME}://{kind}/{{{identifier}}}")


def _records() -> dict[str, tuple[UriTemplate, ResourceSpec]]:
    """Each kind with records to read one at a time, and its template, whoever the caller."""

    return {
        name: (record_template(name, spec.identifier), spec)
        for name, spec in registry.resource_registry().items()
        if spec.detail_handler and spec.identifier
    }


@dataclass(frozen=True, slots=True)
class View:
    name: str
    title: str
    description: str
    read: Callable[[Principal], Mapping[str, Any]]
    # Who may read it: a read is refused by it, and resources/list lists the
    # view only to a caller it admits, as tools/list lists a tool.
    requires: Access

    @property
    def uri(self) -> str:
        return VIEW.expand({"view": self.name})


def action_queue(principal: Principal) -> ResourceCollection:
    """The whole queue, page after page, as its kind's list_fields keep each
    item: what a person reads it by, and its page in HQ."""

    page = registry.list_resource("action_items", None, principal=principal)
    items = list(page["items"])
    while after := page.get("next"):
        page = registry.list_resource("action_items", {PAGE_FIELD: after["cursor"]}, principal=principal)
        items += page["items"]
    return cast(ResourceCollection, complete(items))


def hq_itself(principal: Principal) -> ResourceCollection:
    return registry.list_resource("hq", None, principal=principal)


def held_requests(principal: Principal) -> dict[str, Any]:
    """The changes waiting for a person that ``principal`` may read: an agent's own, or every one for a person."""

    items = [serialize_held(held) for held in approvals.pending(principal=principal)]
    return {"items": items, "count": len(items)}


def serialize_held(held: Any) -> dict[str, Any]:
    return {"asks": str(approval_review.asked(held)), **approvals.serialize_approval(held)}


VIEWS = {
    view.name: view
    for view in (
        View(
            "action_items", "Needs you", "HQ's action queue: what needs doing, from every domain.", action_queue, READS
        ),
        View(
            "approvals",
            "Held requests",
            "The changes you asked for that wait for a person; a person reads every one.",
            held_requests,
            READS,
        ),
        View(
            "hq",
            "HQ",
            (
                "HQ itself: its release, where it runs, its jobs and its open problems. "
                "`system_health` says whether it answers; the `status-brief` prompt is a brief of both."
            ),
            hq_itself,
            READS,
        ),
    )
}


def listed(principal: Principal) -> types.ListResourcesResult:
    """The fixed views ``principal`` may read, each by the rule its read is refused by."""

    return types.ListResourcesResult(
        resources=[
            types.Resource(uri=view.uri, name=view.name, title=view.title, description=view.description, mime_type=MIME)
            for view in VIEWS.values()
            if view.requires.admits(principal)
        ]
    )


def templates(principal: Principal) -> types.ListResourceTemplatesResult:
    """A template for each kind with records to read one at a time that the caller may read."""

    return types.ListResourceTemplatesResult(
        resource_templates=[
            types.ResourceTemplate(
                uri_template=template.template,
                name=name,
                title=spec.label,
                description=f"{spec.summary} One record, by its {spec.identifier}.",
                mime_type=MIME,
            )
            for name, (template, spec) in _records().items()
            if registry.readable(spec, principal)
        ]
    )


# The most identifiers one completion offers: the spec's cap on a completion's
# values, and the largest page a listing serves, so one read answers it.
COMPLETIONS = 100


def completed(uri_template: str, argument: str, typed: str, principal: Principal) -> types.Completion:
    """The identifiers a template's record may be named by that start with
    ``typed``, read from its kind's listing as ``principal``, as ``list_resource``
    reads it; nothing for a kind the caller may not read or cannot list."""

    spec = next((spec for template, spec in _records().values() if template.template == uri_template), None)
    if (
        spec is None
        or argument != spec.identifier
        or not (spec.list_handler and spec.list_query_type)
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

    for kind, (template, _spec) in _records().items():
        # A record's template names one variable, its identifier.
        identifier = next(iter((template.match(uri) or {}).values()), "")
        if isinstance(identifier, str) and identifier:
            # Text, as an address carries it: the kind's own identifier type parses it.
            return registry.get_resource(kind, identifier, principal=principal, strict=False)
    view = VIEWS.get(str((VIEW.match(uri) or {}).get("view", "")))
    if view is None:
        raise _nothing_at(uri)
    view.requires.check(principal)
    return view.read(principal)
