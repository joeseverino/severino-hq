"""HQ's machine API as an OpenAPI 3.2 document, derived and never hand-written.

Served at /api/v2/openapi.json with whatever extensions this deployment
composes; ``hq-api.openapi.json`` beside this module is the host alone,
committed for clients. Each path is a route of the API's URLconf, its
operations and answers its view's ``_endpoint`` declaration, each capability
and resource its registry's entry, and the tag tree the domain registry's.
"""

import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from http import HTTPStatus
from pathlib import Path
from typing import Any, TypeAliasType, get_args

from django.urls import URLPattern, reverse

from hq.platform.application.capabilities import capability_registry, describe_capabilities
from hq.platform.application.contracts import effect_glossary
from hq.platform.application.field_masks import FIELD_PATH_PATTERN, WHOLE_RECORD
from hq.platform.application.held_answer import HELD, AwaitingApproval
from hq.platform.application.integration_specs import (
    FIELD_MASK,
    IDEMPOTENCY_FIELD_SCHEMA,
    IDEMPOTENCY_HEADER,
    PAGE_FIELD,
    RESERVED_QUERY_FIELD,
    STALE_WRITE_KEYWORD,
    CapabilitySpec,
    answer_type,
    takes_stale_write_token,
    typed_keys,
)
from hq.platform.application.json_schemas import LOCAL, json_schema
from hq.platform.application.refusal_status import LATER_CODES
from hq.platform.application.resource_operations import RunAsked
from hq.platform.application.resources import describe_resources, mask_paths, masked_kind, resource_registry
from hq.platform.application.run_reads import RUNS
from hq.platform.application.security import API_INTERFACE, Principal
lazy from hq.platform.application.routes import routed

from . import urls, views
from .components import SCHEMAS, Components
from .fragments import OpenAPIError, camel as _camel, pascal as _pascal
from .refusal_names import LATER_STATUSES, codes_by_status, refusal_name
from .tags import API_TAG, Tags

OPENAPI_VERSION = "3.2.0"
DOCUMENT_PATH = Path(__file__).with_name("hq-api.openapi.json")
PARAMETERS = "#/components/parameters/"
RESPONSES = "#/components/responses/"
VERSION = views.CURRENT_API_VERSION
_PARAMETER = re.compile(r"<(?:\w+:)?(\w+)>")


def routes() -> Iterator[tuple[str, URLPattern]]:
    """Every hq_api route with its full path, wherever the project mounts it."""

    ours = {id(pattern) for pattern in urls.urlpatterns}
    for route in routed():
        if id(route.pattern) in ours:
            yield route.path, route.pattern


def _view(pattern: URLPattern) -> Any:
    """The view, with the attributes ``views._endpoint`` set on it."""

    return pattern.callback


def path_template(route: str) -> str:
    """A Django route as an OpenAPI path: ``<str:name>`` becomes ``{name}``."""

    return _PARAMETER.sub(r"{\1}", route)


def _json_content(schema: dict[str, Any]) -> dict[str, Any]:
    return {"application/json": {"schema": schema}}


class _Builder:
    def __init__(self) -> None:
        self.components = Components()
        self.paths: dict[str, dict[str, Any]] = {}
        self.operation_ids: set[str] = set()
        self.data = self.components.typed(
            {
                view.__name__: view.__hq_data__
                for _, pattern in routes()
                if (view := _view(pattern)).__hq_data__ is not None
            }
        )
        # The envelopes each answer's schema is drawn from.
        self.components.typed({"Failure": views.Failure, "Success": views.Success})
        self.responses: dict[str, Any] = {}
        self.parameters: dict[str, Any] = {}
        self.headers: dict[str, Any] = {}
        # Each request body's first name, by what it says.
        self.requests: dict[str, str] = {}
        # What a token could ever run: /api/v2 is called with tokens, the
        # widest of them one a person signed in (``security.NEEDS_A_PERSON``).
        widest = Principal("openapi", API_INTERFACE, frozenset(), person_key="openapi")
        self.capabilities = describe_capabilities(principal=widest)["capabilities"]
        self.resources = [describe_resources(name=name)["resources"][0] for name in resource_registry()]
        self.resource_specs = resource_registry()
        self.tags = Tags(self.resource_specs, capability_registry())

    def _responses(
        self,
        view: Any,
        codes: Iterable[str],
        answer: tuple[str, dict[str, Any]] | None,
        held: Any,
        links: dict[str, Any],
    ) -> dict[str, Any]:
        schema: dict[str, Any]
        if view.__hq_data__ is None and answer is None:
            # This document itself.
            schema = {
                "type": "object",
                "properties": {
                    "openapi": {
                        "description": "The OpenAPI version the document is written in.",
                        "type": "string",
                        "pattern": r"^3\.2\.\d+$",
                    }
                },
                "required": ["openapi", "info", "paths"],
            }
        else:
            envelope = self.components.schemas["Success"]
            data = answer[1] if answer is not None else self.data.get(view.__name__, {})
            # An answer written in place (a page) keeps the envelope's words for ``data``.
            if "$ref" not in data:
                data = {**envelope["properties"]["data"], **data}
            schema = {**envelope, "properties": {**envelope["properties"], "data": data}}
        # Named for the type its data is, which every view answering it shares.
        kind = view.__hq_data__
        name = (
            f"{answer[0]}Success"
            if answer is not None
            else f"{_pascal(view.__name__)}Success"
            if kind is None
            else f"{kind.__name__}Success"
            if isinstance(kind, (type, TypeAliasType))
            else "ObjectSuccess"
        )
        # The answer's schema is a component, so an operation whose answer
        # leads somewhere writes out its own response, the links beside the
        # same schema: a Reference Object takes no other field (OpenAPI 3.2.0,
        # section 4.23).
        ran = {
            "description": "Success.",
            "headers": self.answered(),
            "content": _json_content({"$ref": self.components.add(name, schema)}),
        }
        success = {**ran, "links": links} if links else self.component("responses", self.responses, name, ran)
        responses: dict[str, Any] = {"200": success}
        if held is not None:
            responses[str(views.HELD_STATUS)] = self.held(held)
        for status, answered in codes_by_status(codes).items():
            responses[str(status)] = {"$ref": RESPONSES + self.refusal(status, answered)}
        return responses

    def held(self, kind: Any) -> dict[str, Any]:
        """The answer at ``views.HELD_STATUS`` of a call held for a person, its data ``kind``."""

        name, phrase = kind.__name__, _pascal(views.HELD_STATUS.phrase)
        envelope = self.components.schemas["Success"]
        data = self.components.typed({name: kind})[name]
        schema = {**envelope, "properties": {**envelope["properties"], "data": data}}
        response = {
            "description": "Accepted: held for approval in HQ. Nothing has happened yet.",
            "headers": self.answered(),
            "content": _json_content(schema),
        }
        if links := self.links(answering=kind):
            response["links"] = links
        return self.component("responses", self.responses, f"{name}{phrase}", response)

    def refusal(self, status: int, codes: list[str]) -> str:
        """The response, and its schema, for refusing at ``status`` with ``codes``, named for them."""

        name = refusal_name(status, codes)
        schema = f"{name}Refusal"
        envelope = self.components.schemas["Failure"]
        detail = self.components.schemas["ErrorDetail"]
        properties = dict(detail["properties"])
        properties["code"] = {**properties["code"], **({"const": codes[0]} if len(codes) == 1 else {"enum": codes})}
        later = status in LATER_STATUSES
        # Only a code that says "later" carries a wait; where every code here does, it always comes.
        waits = later and set(codes) <= LATER_CODES
        if not later:
            del properties["retry_after"]
        # The envelope's words for ``error`` stay with the narrowed copy of its type.
        error = {**detail, "properties": properties, "description": envelope["properties"]["error"]["description"]}
        if waits:
            error["required"] = [*detail["required"], "retry_after"]
        self.components.add(schema, {**envelope, "properties": {**envelope["properties"], "error": error}})
        headers = self.answered()
        if status == HTTPStatus.UNAUTHORIZED:
            headers["WWW-Authenticate"] = {
                "description": "How to authenticate.",
                "required": True,
                "schema": {"type": "string", "const": views.REALM},
            }
        if "insufficient_scope" in codes:
            # One component every 403 that can answer the code shares.
            headers["WWW-Authenticate"] = self.component(
                "headers",
                self.headers,
                _pascal("insufficient_scope") + _pascal("WWW-Authenticate"),
                {
                    "description": (
                        'On `insufficient_scope`: `Bearer` with `error="insufficient_scope"` and `scope`, the '
                        "permission `error.details.scope` names (RFC 6750 section 3.1)."
                    ),
                    "schema": {"type": "string"},
                },
            )
        if later:
            headers["Retry-After"] = {
                "description": "Whole seconds to wait before retrying, the same as `error.retry_after`.",
                "required": waits,
                "schema": {"type": "integer", "minimum": 1},
            }
        response: dict[str, Any] = {
            "description": f"{HTTPStatus(status).phrase}.",
            "headers": headers,
            "content": _json_content({"$ref": SCHEMAS + schema}),
        }
        if self.responses.setdefault(name, response) != response:
            raise OpenAPIError(f"Two responses claim the component name {name!r}.")
        return name

    def answered(self) -> dict[str, Any]:
        """The headers every answer carries: an answer for one bearer is stored by no cache."""

        return {
            "Cache-Control": self.component(
                "headers",
                self.headers,
                "CacheControl",
                {
                    "description": "Operational data read with a bearer credential, correct only for the caller.",
                    "required": True,
                    "schema": {"type": "string", "const": views.ANSWER_CACHE_CONTROL},
                },
            )
        }

    @staticmethod
    def component(section: str, held: dict[str, Any], name: str, value: dict[str, Any]) -> dict[str, Any]:
        """``value``, kept as the component ``name`` of ``section``, and the ``$ref`` that uses it."""

        if held.setdefault(name, value) != value:
            raise OpenAPIError(f"Two {section} claim the component name {name!r}.")
        return {"$ref": f"#/components/{section}/{name}"}

    def links(
        self, *, resource: str = "", listed: bool = False, query: Iterable[str] = (), answering: Any = None
    ) -> dict[str, Any]:
        """Each request an answer leads to, as Link Objects (OpenAPI 3.2.0,
        section 4.20) keyed by the operation they call.

        Derived from the registries: a ``READ_BY`` answer, a page's next page
        and first item, and each command that takes the record as its target.
        """

        found: dict[str, Any] = {}
        members = set(get_args(answering) or (answering,))
        for kind, (read, pointer) in READ_BY.items():
            if kind in members and (spec := self.resource_specs.get(read)) and spec.detail_handler is not None:
                found[_read_id(read)] = _link(_read_id(read), {"path.identifier": f"$response.body#/data{pointer}"})
        if not resource:
            return found
        spec = self.resource_specs[resource]
        query = tuple(query)
        if listed and PAGE_FIELD in query:
            parameters = {f"query.{name}": f"$request.query.{name}" for name in query if name != PAGE_FIELD}
            found[_list_id(resource)] = _link(
                _list_id(resource), {f"query.{PAGE_FIELD}": f"$response.body#/data/next/{PAGE_FIELD}", **parameters}
            )
        record = spec.record_type
        carried = (
            (spec.listing.fields if spec.listing else ())
            if listed
            else (typed_keys(record) if record is not None else ())
        )
        identifier = spec.identifier
        if spec.detail_handler is None or identifier not in carried:
            return found
        at = "/data/items/0" if listed else "/data"
        if listed:
            found[_read_id(resource)] = _link(
                _read_id(resource), {"path.identifier": f"$response.body#{at}/{identifier}"}
            )
        registry = capability_registry()
        for command in self.capabilities:
            capability = registry[command["name"]]
            if capability.subject_resource != resource or not capability.target_kind:
                continue
            body = {"target": f"{{$response.body#{at}/{identifier}}}"}
            if takes_stale_write_token(capability.handler) and UPDATED_AT in carried:
                body[STALE_WRITE_KEYWORD] = f"{{$response.body#{at}/{UPDATED_AT}}}"
            link = _link(_capability_id(capability.name), {}, body)
            if capability.target_query:
                said = " and ".join(f"`{field}` is `{value}`" for field, value in capability.target_query)
                link["description"] = f"Only for a record whose {said}."
            found[_capability_id(capability.name)] = link
        return found

    def parameter(self, name: str, value: dict[str, Any]) -> dict[str, Any]:
        return self.component("parameters", self.parameters, name, value)

    def operation(
        self,
        path: str,
        method: str,
        view: Any,
        operation_id: str,
        tag: str | None,
        **fields: Any,
    ) -> None:
        # An expansion's concrete operation can refuse with fewer codes than its view declares,
        # answer a narrower type (its name and the schema of its data), and hold
        # nothing for a person where its view can.
        codes = fields.pop("codes", view.__hq_codes__)
        answer = fields.pop("answer", None)
        held = fields.pop("held", view.__hq_held__)
        # What a call needs: the view's own access, or the one capability or
        # resource a concrete operation serves.
        scopes = fields.pop("scopes", view.__hq_requires__.scopes)
        links = fields.pop("links", {})
        operation_id = _versioned(operation_id)
        if operation_id in self.operation_ids:
            raise OpenAPIError(f"Operation id {operation_id!r} is derived twice.")
        self.operation_ids.add(operation_id)
        # A view's title names it and its docstring says what it answers; an
        # expansion states both for the concrete operation it derives.
        summary = fields.pop("summary", None)
        if summary is None:
            summary = view.__hq_title__
            fields["description"] = " ".join(
                part for part in (views.first_line(view.__doc__), fields.get("description")) if part
            )
        operation: dict[str, Any] = {
            "operationId": operation_id,
            "summary": summary,
            **({"tags": [tag]} if tag else {}),
            **fields,
            "security": [{BEARER: list(scopes)}],
            "responses": self._responses(view, codes, answer, held, links),
        }
        self.paths.setdefault(path, {})[method.lower()] = operation

    def route(self, route: str, pattern: URLPattern) -> None:
        view = _view(pattern)
        template = path_template(route)
        expand = EXPANSIONS.get(view.__name__)
        if _PARAMETER.search(route) and expand is None:
            raise OpenAPIError(f"No OpenAPI expansion for parameterized route {route!r}.")
        for method in view.__hq_methods__:
            if expand is not None:
                expand(self, template, method, view)
                continue
            query = [
                _query_parameter(field, schema)
                for field, schema in getattr(view, "__hq_query_fields__", dict)().items()
            ]
            self.operation(
                template,
                method,
                view,
                _camel(view.__name__),
                self.tags.of_route(view.__hq_page__),
                **({"parameters": query} if query else {}),
                **(
                    {"description": "Also served to the signed-in operator's web session."}
                    if view.__hq_operator_session__
                    else {}
                ),
            )

    # Expansions: one templated operation for the route itself, then one
    # concrete path per registry entry it serves. OpenAPI matches a concrete
    # path before its template.

    def retry_key(self, *, required: bool) -> dict[str, Any]:
        """The header a change's retry key is sent in, the one way the machine API takes it."""

        schema = {"$ref": self.components.add(IDEMPOTENCY_KEY, dict(IDEMPOTENCY_FIELD_SCHEMA))}
        return {"name": IDEMPOTENCY_HEADER, "in": "header", "required": required, "schema": schema}

    def capability_paths(self, template: str, method: str, view: Any) -> None:
        generic = self.components.hoist(views.request_schema({"input_schema": {"type": "object"}, "target": None}))
        generic["properties"]["target"] = views.ENVELOPE_SCHEMA["properties"]["target"]
        self.operation(
            template,
            method,
            view,
            _camel(view.__name__),
            self.tags.use(API_TAG),
            # A parameter cannot depend on the path's name, and this path runs
            # reads too, so here the key every change's own operation requires
            # is taken, never required.
            parameters=[
                _named("capability", [spec["name"] for spec in self.capabilities], "call"),
                self.retry_key(required=False),
            ],
            requestBody=_body(generic),
        )
        registry = capability_registry()
        requests = {spec["name"]: self.components.hoist(views.request_schema(spec)) for spec in self.capabilities}
        # A target several capabilities take in the same words is written once,
        # as the component named for the first of them.
        targets: dict[str, list[str]] = {}
        for name, request in requests.items():
            if "target" in request["properties"]:
                targets.setdefault(json.dumps(request["properties"]["target"], sort_keys=True), []).append(name)
        for said, takers in targets.items():
            if len(takers) > 1:
                target = json.loads(said)
                # An id target is a choice between a number and its digits, and
                # a viewer offering the two says what they are, never the name
                # the component is filed under.
                if "oneOf" in target:
                    target["title"] = "An id"
                shared = {"$ref": self.components.add(f"{_pascal(takers[0])}Target", target)}
                for name in takers:
                    requests[name] = {
                        **requests[name],
                        "properties": {**requests[name]["properties"], "target": shared},
                    }
        # A command several capabilities take in one shape is the component
        # named for the type of the first of them, so commands of different
        # types written alike (two taking no field but the retry key, which
        # travels in its header) share it too; one taken in a shape of its own
        # (an update's shows no defaults) stays in its body.
        shapes: dict[str, list[str]] = {}
        for name, request in requests.items():
            shapes.setdefault(json.dumps(request["properties"]["payload"], sort_keys=True), []).append(name)
        for spec in self.capabilities:
            name = spec["name"]
            request = requests[name]
            payload = request["properties"]["payload"]
            if len(takers := shapes[json.dumps(payload, sort_keys=True)]) > 1:
                shared = {"$ref": self.components.add(registry[takers[0]].command_type.__name__, payload)}
                request = {**request, "properties": {**request["properties"], "payload": shared}}
            # Capabilities taking one body share its schema, each under its own name.
            first = self.requests.setdefault(json.dumps(request, sort_keys=True), f"{_pascal(name)}Request")
            shared = request if first == f"{_pascal(name)}Request" else {"$ref": SCHEMAS + first}
            body = {"$ref": self.components.add(f"{_pascal(name)}Request", shared)}
            self.operation(
                template.replace("{name}", name),
                method,
                view,
                _camel(name),
                self.tags.of_capability(name),
                links=self.links(answering=answer_type(registry[name])),
                summary=spec["label"],
                description=spec["summary"],
                parameters=([{"$ref": f"#/components/parameters/{IDEMPOTENCY_KEY}"}] if spec["effect"].changes else []),
                requestBody=_body(body),
                codes=views.execute_codes(registry[name]),
                answer=self.answer_of(registry[name]),
                held=views.capability_held(registry[name]),
                scopes=spec["required_capabilities"],
                **{"x-hq-capability": name, EFFECT_EXTENSION: spec["effect"]},
            )

    def answer_of(self, spec: CapabilitySpec) -> tuple[str, dict[str, Any]] | None:
        """A capability's answer where its handler names one, under the name of its result type.

        None leaves the view's object, for a handler whose result is a plain mapping.
        """

        data = views.capability_data(spec)
        if data is None:
            return None
        name = "".join(kind.__name__ for kind in get_args(data)) or data.__name__
        return name, self.components.typed({spec.name: data})[spec.name]

    def resource_list_paths(self, template: str, method: str, view: Any) -> None:
        self.operation(
            template,
            method,
            view,
            _camel(view.__name__),
            self.tags.use(API_TAG),
            parameters=[
                _named("resource", [spec["name"] for spec in self.resources if spec["operations"]["list"]], "list"),
                _FIELDS,
            ],
        )
        for spec in self.resources:
            listing = spec["operations"]["list"]
            if listing is None:
                continue
            name = spec["name"]
            # The machine API reads a list's paging from the query string, beside its filters.
            parts = [listing["query_schema"], *([listing["paging"]] if "paging" in listing else [])]
            query = [field for part in parts for field in part.get("properties", {})]
            self.operation(
                template.replace("{name}", name),
                method,
                view,
                _list_stem(name),
                self.tags.of_resource(name),
                # Titled as a read of one is, so it never reads as its section's
                # name or as a bare verb.
                summary=f"{spec['label']}: the list",
                description=spec["summary"],
                # Emit usable client filters and retain the whole strict query contract.
                parameters=[
                    self.filter(part, field, required=field in part.get("required", []))
                    for part in parts
                    for field in part.get("properties", {})
                ]
                + [self.mask(name, listed=True)],
                answer=self.page_of(name),
                links=self.links(resource=name, listed=True, query=[*query, RESERVED_QUERY_FIELD]),
                scopes=spec["required_capabilities"],
                **{"x-hq-resource": name},
            )

    def typed(self, kind: type) -> dict[str, Any]:
        """The ``$ref`` to a resource's list item or record, which a caller's field mask cuts
        (``Components.written``)."""

        name = kind.__name__
        if name not in self.components.schemas:
            self.components.typed({name: kind})
        return self.components.mask(name)

    def page_of(self, name: str) -> tuple[str, dict[str, Any]] | None:
        """A list's answer: every list's page, its items of the type the
        resource declares, which a mask cuts and never widens."""

        kind = masked_kind(self.resource_specs[name], listed=True)
        if kind is None:
            return None
        page = self.data[views.resource_list.__name__]
        items = {"type": "array", "items": self.typed(kind)}
        return f"{kind.__name__}Page", {"allOf": [page], "properties": {"items": items}}

    def record_of(self, record: type | None) -> tuple[str, dict[str, Any]] | None:
        """A read of one record's answer, where its kind types it; else the view's object."""

        return None if record is None else (record.__name__, self.typed(record))

    def filter(self, query: dict[str, Any], field: str, *, required: bool) -> dict[str, Any]:
        """The query parameter for ``field`` of a list's query schema.

        A field typed by a named alias is the parameter component of that
        name, written once for every list that takes it; the alias's words
        are the parameter's.
        """

        schema = query["properties"][field]
        ref = schema.get("$ref", "")
        alias = ref.removeprefix(LOCAL) if ref.startswith(LOCAL) else ""
        if alias:
            schema = {**query["$defs"][alias], **{key: value for key, value in schema.items() if key != "$ref"}}
        # What the field's type names beyond itself (a nested definition) is a schema component.
        definitions = {key: value for key, value in query.get("$defs", {}).items() if key != alias}
        parameter = _query_parameter(
            field,
            self.components.hoist({**schema, "$defs": definitions}),
            required=required,
            style="form",
            explode=True,
        )
        return self.parameter(alias, parameter) if alias else parameter

    def mask(self, name: str, *, listed: bool) -> dict[str, Any]:
        """The ``fields`` parameter of a read of resource ``name``: the field paths of what it answers, kept
        as the parameter component named for their type."""

        spec = self.resource_specs[name]
        declared = masked_kind(spec, listed=listed)
        if declared is None:
            return _FIELDS
        paths = mask_paths(spec, listed=listed)
        if listed and masked_kind(spec, listed=False) and set(paths) == set(mask_paths(spec, listed=False)):
            # A list whose item's paths are its record's keeps what one record's read keeps.
            return self.mask(name, listed=False)
        kind = f"{declared.__name__}{'Page' if listed else ''}Fields"
        return self.parameter(kind, _mask({"type": "string", "enum": [*paths, WHOLE_RECORD]}))

    def resource_detail_paths(self, template: str, method: str, view: Any) -> None:
        self.operation(
            template,
            method,
            view,
            _camel(view.__name__),
            self.tags.use(API_TAG),
            parameters=[
                # A resource found by other than text is read by its own path alone,
                # where its identifier is typed as it declares it.
                _named(
                    "resource",
                    [
                        spec["name"]
                        for spec in self.resources
                        if spec["operations"]["get"]
                        and json_schema(self.resource_specs[spec["name"]].identifier_type) == _TEXT
                    ],
                    "read of one record",
                ),
                _path_parameter("identifier", _TEXT, "The record's value of the field its resource is found by."),
                _FIELDS,
            ],
        )
        for spec in self.resources:
            detail = spec["operations"]["get"]
            if detail is None:
                continue
            name = spec["name"]
            field = detail["identifier"]
            identifier = json_schema(self.resource_specs[name].identifier_type)
            listed = spec["operations"]["list"] is not None
            # A resource with a list holds the records its summary describes,
            # and this reads one of them; one without is read one at a time,
            # and its summary is already about one.
            self.operation(
                template.replace("{name}", name),
                method,
                view,
                _read_stem(name),
                self.tags.of_resource(name),
                summary=f"{spec['label']}: one by {field.replace('_', ' ')}",
                description=(
                    f"One of the records {spec['label']} lists, found by its `{field}`." if listed else spec["summary"]
                ),
                parameters=[
                    # Every resource read by one field takes it alike.
                    self.parameter(
                        f"{_pascal(field)}Identifier" if listed else f"{_pascal(field)}Lookup",
                        _path_parameter(
                            "identifier",
                            identifier,
                            f"The record's `{field}`, as each item of the list carries it."
                            if listed
                            else f"The `{field}` to read it for.",
                        ),
                    ),
                    self.mask(name, listed=False),
                ],
                answer=self.record_of(self.resource_specs[name].record_type),
                links=self.links(resource=name),
                scopes=spec["required_capabilities"],
                **{"x-hq-resource": name, "x-hq-identifier": field},
            )


_FIELDS = {"$ref": "#/components/parameters/Fields"}
_TEXT = {"type": "string"}
# The one security scheme; an operation's requirement of it lists the capabilities a call needs
# (OpenAPI 3.2, Security Requirement Object: role names, for a scheme other than OAuth's).
BEARER = "bearer"
IDEMPOTENCY_KEY = "IdempotencyKey"
# Where a capability operation says its effect, which the document's introduction explains.
EFFECT_EXTENSION = "x-hq-effect"


# The answers that carry what a read finds, each by that read's resource and
# where in the answer's data its identifier stands.
READ_BY: dict[Any, tuple[str, str]] = {AwaitingApproval: (HELD, "/approval/id"), RunAsked: (RUNS, "/operation/id")}
UPDATED_AT = "updated_at"


def _versioned(stem: str) -> str:
    return f"{stem}V{VERSION}"


def _list_stem(resource: str) -> str:
    return f"list{_pascal(resource)}"


def _read_stem(resource: str) -> str:
    return f"get{_pascal(resource)}"


def _list_id(resource: str) -> str:
    return _versioned(_list_stem(resource))


def _read_id(resource: str) -> str:
    return _versioned(_read_stem(resource))


def _capability_id(capability: str) -> str:
    return _versioned(_camel(capability))


def _link(operation_id: str, parameters: dict[str, str], body: dict[str, str] | None = None) -> dict[str, Any]:
    link: dict[str, Any] = {"operationId": operation_id}
    if parameters:
        link["parameters"] = parameters
    if body:
        link["requestBody"] = body
    return link


EXPANSIONS: dict[str, Callable[[_Builder, str, str, Any], None]] = {
    views.execute.__name__: _Builder.capability_paths,
    views.resource_list.__name__: _Builder.resource_list_paths,
    views.resource_detail.__name__: _Builder.resource_detail_paths,
}


def _query_parameter(name: str, schema: Mapping[str, Any], *, required: bool = False, **style: Any) -> dict[str, Any]:
    """A query parameter for one field of a query's schema, its words on the parameter rather than its schema.

    A field not sent is absent, so a ``null`` its model allows is no value the
    parameter can be, and a ``null`` default no default it has.
    """

    said = {key: value for key, value in schema.items() if key not in {"description", "anyOf"}}
    if said.get("default", ...) is None:
        del said["default"]
    branches = [branch for branch in schema.get("anyOf", ()) if branch != {"type": "null"}]
    if len(branches) == 1:
        said = {**branches[0], **said}
    elif branches:
        said["anyOf"] = branches
    parameter: dict[str, Any] = {"name": name, "in": "query", "required": required, **style, "schema": said}
    if "description" in schema:
        parameter["description"] = schema["description"]
    return parameter


def _mask(path: dict[str, Any]) -> dict[str, Any]:
    """The ``fields`` query parameter whose every item is a ``path``."""

    return {
        "name": RESERVED_QUERY_FIELD,
        "in": "query",
        "required": False,
        "description": f"{FIELD_MASK} Sent as one comma-separated value (`observed_at,items.waiting`).",
        "style": "form",
        "explode": False,
        "schema": {"type": "array", "items": path, "minItems": 1},
    }


def _named(kind: str, names: list[str], operation: str) -> dict[str, Any]:
    """The ``{name}`` of a route served for every ``kind`` in ``names``, each with an operation of its own.

    It says which operation it stands for, so the list's and the read's are
    two parameters even where no resource serves either.
    """

    return _path_parameter(
        "name",
        {"type": "string", "enum": sorted(names)},
        f"A {kind}'s name. This is the generic form of the {operation} each {kind} has under its own path.",
    )


def _path_parameter(name: str, schema: dict[str, Any], description: str) -> dict[str, Any]:
    return {
        "name": name,
        "in": "path",
        "required": True,
        "description": description,
        "schema": schema,
    }


def _body(schema: dict[str, Any]) -> dict[str, Any]:
    # Required: the view refuses a request that is not application/json, so a
    # body-less call is a 415, not an empty command. An empty command is `{}`.
    return {"required": True, "content": _json_content(schema)}


def document() -> dict[str, Any]:
    """The OpenAPI 3.2 document for this deployment's machine API."""

    builder = _Builder()
    for route, pattern in routes():
        builder.route(route, pattern)
    return {
        "openapi": OPENAPI_VERSION,
        "$self": reverse("hq_api:openapi"),
        "jsonSchemaDialect": "https://json-schema.org/draft/2020-12/schema",
        "info": {
            "title": "Severino HQ machine API",
            "version": str(VERSION),
            "description": views.first_line(views.__doc__)
            + " Derived from HQ's routes and its capability, resource and domain registries.\n\n"
            + effect_glossary(EFFECT_EXTENSION),
        },
        "tags": builder.tags.render(),
        "paths": dict(sorted(builder.paths.items())),
        "components": {
            "securitySchemes": {
                BEARER: {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "JWT",
                    "description": (
                        "An access token from HQ's identity provider, addressed to this API. The names an "
                        "operation's security lists are the HQ capabilities its token must carry, every one."
                    ),
                }
            },
            "parameters": dict(
                sorted(
                    {
                        IDEMPOTENCY_KEY: builder.retry_key(required=True),
                        **builder.parameters,
                        "Fields": _mask({"type": "string", "pattern": FIELD_PATH_PATTERN}),
                    }.items()
                )
            ),
            "responses": dict(sorted(builder.responses.items())),
            "headers": dict(sorted(builder.headers.items())),
            "schemas": dict(sorted(builder.components.written().items())),
        },
    }


def render(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"
