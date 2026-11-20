"""Discoverable, authorized read resources shared by every HQ adapter."""

import re
from collections.abc import Callable, Iterable, Mapping
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from django.core import signing
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    with_config,
)

from hq.platform.core.digests import digest

from .answer_fields import Permitted, Pinned, RequiredPermissions, SchemaVersion, WebRoute
from .contracts import Closed
from .describe import Describe
from .field_masks import FieldMaskRefusal, Fields, asks_whole, field_paths, selected
from .input_errors import pydantic_refusal
from .integration_specs import PAGE_FIELD, ResourceSpec, typed_keys
from .integration_validation import required_capability_names
from .integrations import held_graph
from .json_schemas import narrowed, sent_schema
from .projection import MAX_PAGE_SIZE, CursorRefusal, NextPage, PageScope, page_size, paging
from .record_errors import HostRefusal, RecordNotFound
from .refusal_status import next_call
from .resource_refusals import (
    FOUND_BY,
    ResourceError,
    UnsupportedResourceOperation,
    missed,
    undetailed,
    unknown,
    unlisted,
)
from .search_contracts import SearchDefinition
from .security import Capability, Principal


class ResourceQuery(Closed):
    """Base for list inputs: unknown fields are always programmer errors."""


def limit_words(most: int, *, paged: bool) -> str:
    """What a list query's ``limit`` says, its bound from the value it is checked by."""

    return (
        f"How many records a page holds, 1 to {most}; next.cursor reads the page after."
        if paged
        else f"How many records to return, 1 to {most}, first by the list's own ranking."
    )


# Each list's ``limit`` is a named alias, so the API document writes it once
# for every query that takes it; a query whose default differs takes an alias
# of its own, since the parameter the document writes carries the default.
# The fields every paged list takes:
type PageLimit = Annotated[int, Field(ge=1, le=MAX_PAGE_SIZE, description=limit_words(MAX_PAGE_SIZE, paged=True))]
type Cursor = Annotated[
    str | None,
    Field(
        max_length=4000,
        description=(
            "The page after the one whose next.cursor this is. Send it as it came, with the filters that page was "
            "read with; absent, the first page."
        ),
    ),
]


# What a list's own ``status`` and ``query`` narrow by, wherever a query declares them.
STATUS_FILTER = "Only the records in this status; absent, every status."
TEXT_FILTER = "Only the records whose text holds this, ignoring case; absent, every record."
# A list's ``query`` where it may be left out.
type TextFilter = Annotated[str | None, Field(description=TEXT_FILTER)]
# A list's ``status`` where it may be left out and its statuses are not
# declared as a choice; one that declares them types the field by ``one_of``.
type StatusFilter = Annotated[str | None, Field(description=STATUS_FILTER)]
# A ranked list's ``limit``: the deployment-wide ceiling, declared so a caller
# reads it in the schema and a page past it is refused rather than quietly cut.
type TopLimit = Annotated[int, Field(ge=1, le=MAX_PAGE_SIZE, description=limit_words(MAX_PAGE_SIZE, paged=False))]


# The fields that page a list rather than narrow it: MCP's list_resource
# takes them beside its filters, and a listing describes them apart from its
# filters, as its ``paging``.
PAGING = frozenset({"limit", PAGE_FIELD})


def list_filters(query: type[BaseModel]) -> tuple[str, ...]:
    """What a list query narrows by: every field but the ones that page it."""

    return tuple(name for name in query.model_fields if name not in PAGING)


class TopQuery(ResourceQuery):
    """A list's first ``limit`` by its own ranking, which a read narrows rather than pages past."""

    limit: TopLimit = 50


class BoundedQuery(TopQuery):
    """A list read one page at a time, every row reached by following each page's ``next``."""

    limit: PageLimit = 50
    cursor: Cursor = None


def one_of(values: Iterable[str]) -> Any:
    """A ``Literal`` of ``values``, taken from where they are declared: a query
    field typed by it lists them as its schema's enum and refuses any other,
    naming the field.

    Built at run time, so a query model declares such a field through
    ``create_model``: a type checker reads a ``Literal`` only from literals.
    """

    chosen = tuple(dict.fromkeys(values))
    if not chosen:
        raise ValueError("A choice needs at least one value to choose.")
    return Literal[chosen]


def several_of(values: Iterable[str]) -> str:
    """The pattern of a comma-separated text whose every part is one of
    ``values``: a query field held to it lists them in its schema and refuses
    any other part, as ``one_of`` does for one."""

    either = "|".join(re.escape(value) for value in dict.fromkeys(values))
    return rf"^(?:{either})(?:,(?:{either}))*$"


class EmptyQuery(ResourceQuery):
    pass


class InvalidResourceInput(ResourceError):
    """Input a resource's read refused, said once for every door: each field
    that was wrong and why, the filters its kind's list takes, and the call
    that says each one's type, so the next call can be right."""

    def __init__(self, name: str, errors: Iterable[Mapping[str, Any]]) -> None:
        refusal = pydantic_refusal(name, errors)
        spec = resource_registry().get(name)
        details: dict[str, Any] = {"errors": refusal.details, "next": next_call("describe_resources", name=name)}
        if spec is not None and spec.listing:
            details["filters"] = list(list_filters(spec.listing.query))
        super().__init__(refusal.message)
        self.details = details


def _held(spec: ResourceSpec, principal: Principal) -> list[object]:
    """The identifiers on the first page of ``spec``'s list read as
    ``principal``, so a miss never names a record the caller could not list."""

    if not (spec.listing and spec.identifier):
        return []
    query = {"limit": MAX_PAGE_SIZE} if "limit" in spec.listing.query.model_fields else {}
    try:
        page = list_resource(spec.name, query, principal=principal, fields=[spec.identifier])
    except HostRefusal, ResourceError:
        # A list that needs a filter only the caller can choose, or whose
        # rows do not carry the identifier, holds no candidates.
        return []
    return [item[spec.identifier] for item in page["items"] if spec.identifier in item]


# Each resource tool, and the refusal of a resource that does not serve its read.
_TOOL_READS: dict[str, Callable[[ResourceSpec], UnsupportedResourceOperation | None]] = {
    "describe_resources": lambda _spec: None,
    "list_resource": lambda spec: None if spec.listing else unlisted(spec),
    "get_resource": lambda spec: None if spec.detail_handler and spec.identifier else undetailed(spec),
}


def name_refusal(tool: str, name: object) -> HostRefusal | None:
    """What ``tool`` raises for a resource ``name`` it cannot read, for a door
    that checks the name first; None for any other tool or a name not a string."""

    if tool not in _TOOL_READS or not isinstance(name, str):
        return None
    spec = resource_registry().get(name)
    return unknown(name, resource_registry()) if spec is None else _TOOL_READS[tool](spec)


def resource_registry() -> dict[str, ResourceSpec]:
    return dict(held_graph().resources)


def resource_search_definitions() -> tuple[SearchDefinition, ...]:
    return tuple(held_graph().search.values())


def resource_search_capabilities() -> dict[str, tuple[Capability | str, ...]]:
    return {spec.search.scope: spec.required_capabilities for spec in held_graph().resources.values() if spec.search}


class ListOperation(TypedDict):
    """How a resource is listed, where it can be."""

    filters: Annotated[
        list[str],
        Describe(
            "The fields a listing narrows by, without `limit` and `cursor`, which page it; `describe_resources` "
            "given this name says each one's type and choices."
        ),
    ]
    fields: NotRequired[Annotated[list[str], Describe("The fields each item keeps unless the caller names its own.")]]
    query_schema: NotRequired[
        Annotated[
            dict[str, Any],
            Describe(
                "The JSON Schema of the `filters` a listing takes, each one's type and choices; only in a read of "
                "this one resource."
            ),
        ]
    ]
    paging: NotRequired[
        Annotated[
            dict[str, Any],
            Describe(
                "The JSON Schema of `limit`, with the most it may ask for, and `cursor`, where the list takes them: "
                "`list_resource` takes each as an argument of its own beside `filters`, and the machine API as a "
                "query parameter beside them. Only in a read of this one resource; absent, the list takes neither."
            ),
        ]
    ]


class GetOperation(TypedDict):
    """How one record of a resource is read, where it can be."""

    identifier: Annotated[str, Describe("The field of a record that `get_resource` finds it by.")]


class SearchOperation(TypedDict):
    """How a resource is searched, where it can be."""

    scope: Annotated[str, Describe("The name search takes to look only in this resource.")]


class ResourceOperations(TypedDict):
    """What can be done with a resource: each read it serves, or null."""

    list: ListOperation | None
    get: GetOperation | None
    search: SearchOperation | None


class ResourceDescription(TypedDict):
    """One registry entry as every adapter describes it."""

    name: Annotated[str, Describe("The resource's name, which `list_resource` and `get_resource` take.")]
    label: Annotated[str, Describe("The name a page gives the resource.")]
    summary: Annotated[str, Describe("What the resource holds, in a sentence.")]
    web_route: WebRoute
    required_capabilities: RequiredPermissions
    operations: ResourceOperations
    permitted: NotRequired[Permitted]


# The shape describe_resources answers in; it moves when that shape does.
RESOURCES_SCHEMA_VERSION = 4


class ResourceCatalog(TypedDict):
    """What ``describe_resources`` answers."""

    schema_version: Annotated[SchemaVersion, Pinned(RESOURCES_SCHEMA_VERSION)]
    resources: Annotated[list[ResourceDescription], Describe("Each resource asked for: every one, or the one named.")]


def describe_resources(*, principal: Principal | None = None, name: str = "") -> ResourceCatalog:
    """Every resource HQ serves and its operations, each list by the fields it
    narrows by; or, given ``name``, that one resource with its list's whole
    query schema. Given ``principal``, each entry says whether it is
    ``permitted``, by the rule its read refuses by."""

    if name:
        return {
            "schema_version": RESOURCES_SCHEMA_VERSION,
            "resources": [_described(_resource(name), principal, whole=True)],
        }
    described = [_described(spec, principal, whole=False) for spec in held_graph().resources.values()]
    return {"schema_version": RESOURCES_SCHEMA_VERSION, "resources": described}


def _listing(spec: ResourceSpec, *, whole: bool) -> ListOperation | None:
    if not spec.listing:
        return None
    listing: ListOperation = {"filters": list(list_filters(spec.listing.query))}
    if whole:
        listing["fields"] = list(spec.listing.fields)
        schema = sent_schema(spec.listing.query)
        properties = schema.get("properties", {})
        listing["query_schema"] = narrowed(schema, properties.keys() - PAGING)
        if PAGING & properties.keys():
            listing["paging"] = narrowed(schema, PAGING)
    return listing


def _described(spec: ResourceSpec, principal: Principal | None, *, whole: bool) -> ResourceDescription:
    entry: ResourceDescription = {
        "name": spec.name,
        "label": spec.label,
        "summary": spec.summary,
        "web_route": spec.web_route or None,
        "required_capabilities": list(required_capability_names(spec)),
        "operations": {
            "list": _listing(spec, whole=whole),
            "get": ({"identifier": spec.identifier} if spec.identifier else None),
            "search": ({"scope": spec.search.scope} if spec.search else None),
        },
    }
    if principal is not None:
        entry["permitted"] = readable(spec, principal)
    return entry


def _resource(name: str) -> ResourceSpec:
    try:
        return resource_registry()[name]
    except KeyError as exc:
        raise unknown(name, resource_registry()) from exc


def readable(spec: ResourceSpec, principal: Principal) -> bool:
    """Whether ``principal`` may read ``spec`` (``Requires.permitted_to``), for an adapter, which asks no permission itself."""

    return spec.permitted_to(principal)


def _caller(spec: ResourceSpec, principal: Principal) -> dict[str, Principal]:
    return {"principal": principal} if spec.pass_principal else {}


def list_resource(
    name: str,
    query: dict[str, Any] | None = None,
    *,
    principal: Principal,
    strict: bool = True,
    fields: Fields | None = None,
) -> ResourceCollection:
    """One page of a resource; ``fields`` is a field mask read in each item,
    ``["*"]`` the whole item, and none its kind's ``ListRead.fields``."""

    spec = _resource(name)
    spec.authorize(principal)
    if spec.listing is None:
        raise unlisted(spec)
    read = spec.listing
    try:
        parsed = read.query.model_validate(query or {}, strict=strict)
    except ValidationError as exc:
        raise InvalidResourceInput(name, exc.errors()) from exc
    asked = parsed.model_dump()
    token = asked.pop(PAGE_FIELD, None)
    bound = _bound(parsed)
    with paging(_opened(name, token, bound) if token else None) as scope:
        try:
            result = read.reader(**asked, **_caller(spec, principal))
        except CursorRefusal as exc:
            raise _cursor_refused(name, exc.reason) from exc
    # A schema that offers a cursor is a promise every full page issues one.
    if PAGE_FIELD in type(parsed).model_fields and not scope.claimed:
        raise RuntimeError(f"Resource {name!r} declares a cursor its handler never paged by.")
    invalid = RuntimeError(f"Resource {name!r} list handler returned an invalid collection.")
    try:
        handled = _HANDLED.validate_python(result, strict=True)
    except ValidationError:
        raise invalid from None
    if handled["count"] != len(handled["items"]):
        raise invalid
    page: ResourceCollection = {**handled, **_page(spec, parsed, handled, scope, bound)}
    # Each item holds only what its type declares, whatever its handler built,
    # and by default only its kind's fields of that.
    kept = frozenset(read.fields if fields is None else typed_keys(read.item))
    items = [{key: value for key, value in item.items() if key in kept} for item in page["items"]]
    if fields is not None and not asks_whole(fields):
        items = _masked(spec, items, fields, listed=True)
    return {**page, "items": items}


def every_item(name: str, *, principal: Principal, fields: Fields | None = None) -> list[dict[str, Any]]:
    """Every item of a resource's list, read page after page through
    ``list_resource`` at its largest page: what a web page drawing the whole
    list shows."""

    spec = _resource(name)
    query: dict[str, Any] = (
        {"limit": MAX_PAGE_SIZE} if spec.listing and "limit" in spec.listing.query.model_fields else {}
    )
    items: list[dict[str, Any]] = []
    while True:
        page = list_resource(name, query, principal=principal, strict=False, fields=fields)
        items.extend(page["items"])
        if page["next"] is None:
            return items
        query = {**query, **page["next"]}


class PagePosition(TypedDict):
    """Where a page of a list stopped."""

    total: Annotated[
        int | None, Describe("How many records matched in all, where the list counts them cheaply; else null.")
    ]
    truncated: Annotated[
        bool, Describe("Whether more records follow this page; `next` reads the page after, and is null on the last.")
    ]
    next: NextPage | None


@with_config(ConfigDict(extra="allow"))
class ResourceCollection(PagePosition):
    """One page of a list: every list holds these, and may hold metadata of its own."""

    items: Annotated[
        list[dict[str, Any]], Describe("The page's records, in the list's order, each of its list's item type.")
    ]
    count: Annotated[int, Describe("How many records this page holds.")]


@with_config(ConfigDict(extra="allow"))
class _HandledPage(TypedDict):
    """What a list handler answers, before HQ says where its page stopped."""

    items: list[dict[str, Any]]
    count: int
    total: NotRequired[int | None]
    truncated: NotRequired[bool]


_HANDLED = TypeAdapter(_HandledPage)


def _page(spec: ResourceSpec, parsed: BaseModel, result: _HandledPage, scope: PageScope, bound: str) -> PagePosition:
    """Whether more follow this page, and the cursor that reads the next one.

    ``total`` is what the handler counted, or None where counting is not cheap.
    A page cut through ``projection``'s helpers says where it stopped; one cut
    by hand is read as cut off when its page is full, so a full page is never
    mistaken for the whole list, and offers no cursor, since nothing could
    resume it.
    """

    count, total = result["count"], result.get("total")
    if scope.claimed and PAGE_FIELD in type(parsed).model_fields:
        more = scope.after is not None
        return {
            "total": total,
            "truncated": more,
            "next": {"cursor": _issued(spec.name, bound, scope.after)} if more else None,
        }
    # A list's query model is its declarer's, plugin or host, and need not page.
    limit = getattr(parsed, "limit", None)
    full = isinstance(limit, int) and count >= page_size(limit)
    truncated = result.get("truncated", total > count if isinstance(total, int) else full)
    return {"total": total, "truncated": truncated, "next": None}


# A cursor is signed, so the position in it is one HQ wrote, and carries the
# kind and the filters it was issued under, so a page is never read past a
# position in some other list.
_CURSOR_SALT = "hq.platform.application.resources.cursor"


def _bound(parsed: BaseModel) -> str:
    """The filters a page was read under, as one digest: every query field but the page's size and cursor."""

    terms = parsed.model_dump(mode="json", exclude={"limit", PAGE_FIELD})
    return digest(terms)[:32]


def _issued(name: str, bound: str, position: dict[str, Any] | None) -> str:
    return signing.dumps({"kind": name, "bound": bound, "at": position}, salt=_CURSOR_SALT, compress=True)


def _opened(name: str, token: str, bound: str) -> dict[str, Any]:
    """The position ``token`` resumes from, or a refusal naming the cursor when it
    was not issued by this kind's list under these filters."""

    try:
        opened = signing.loads(token, salt=_CURSOR_SALT)
    except signing.BadSignature as exc:
        raise _cursor_refused(name, "cursor is not one HQ issued; send a page's next.cursor as it came.") from exc
    if not isinstance(opened, dict) or opened.get("kind") != name or not isinstance(opened.get("at"), dict):
        raise _cursor_refused(name, "cursor was issued for another list.")
    if opened.get("bound") != bound:
        raise _cursor_refused(name, "cursor was issued under other filters; send the filters it was issued with.")
    position: dict[str, Any] = opened["at"]
    return position


def _cursor_refused(name: str, reason: str) -> InvalidResourceInput:
    return InvalidResourceInput(name, [{"type": "value_error", "loc": [PAGE_FIELD], "msg": reason}])


def masked_kind(spec: ResourceSpec, *, listed: bool) -> type | None:
    """The type a field mask over ``spec``'s records declares paths from: a
    list's item, or a read of one's record; none where it types neither."""

    if listed:
        return spec.listing.item if spec.listing else None
    return spec.record_type


def mask_paths(spec: ResourceSpec, *, listed: bool) -> tuple[str, ...]:
    """Every field path a mask over ``spec``'s records may name, from ``masked_kind``."""

    kind = masked_kind(spec, listed=listed)
    return () if kind is None else field_paths(kind)


def _masked(spec: ResourceSpec, records: list[Any], fields: Fields, *, listed: bool) -> list[dict[str, Any]]:
    if any(not isinstance(record, dict) for record in records):
        raise TypeError(f"Resource {spec.name!r} returned a record that is not an object.")
    try:
        return selected(records, fields, mask_paths(spec, listed=listed))
    except FieldMaskRefusal as exc:
        raise InvalidResourceInput(spec.name, exc.errors) from exc


def get_resource(
    name: str,
    identifier: object,
    *,
    principal: Principal,
    strict: bool = True,
    fields: Fields | None = None,
) -> dict[str, Any]:
    """One record of a resource; ``fields`` is a field mask over it.

    A record that is not there raises ``RecordNotFound``, whichever handler
    looked for it.
    """

    spec = _resource(name)
    spec.authorize(principal)
    if not spec.detail_handler or not spec.identifier:
        raise undetailed(spec)
    try:
        parsed: Any = TypeAdapter(spec.identifier_type).validate_python(identifier, strict=strict)
    except ValidationError as exc:
        raise InvalidResourceInput(name, exc.errors()) from exc
    try:
        result = spec.detail_handler(parsed, **_caller(spec, principal))
    except RecordNotFound as exc:
        # The host's own sentence, with what finds the record beside it.
        raise missed(exc, spec, parsed, _held(spec, principal)) from None
    except spec.not_found_errors as exc:
        # An extension declares these types; their text is written for the
        # extension, not for a caller, so answer with this module's own.
        refusal = RecordNotFound("No record matches the requested identifier." + FOUND_BY)
        raise missed(refusal, spec, parsed, _held(spec, principal)) from exc
    if not isinstance(result, dict):
        raise TypeError(f"Resource {name!r} detail handler returned a non-object.")
    if fields is None or asks_whole(fields):
        return result
    return _masked(spec, [result], fields, listed=False)[0]
