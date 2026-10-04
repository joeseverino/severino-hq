"""HQ's machine API as an OpenAPI 3.2 document, derived and never hand-written.

Every path comes from hq_api's URLconf, every operation's methods, success type
and error statuses from its view's ``_endpoint`` declaration, every capability
from the capability registry (its request body is the JSON Schema the view
already validates against, ``_request_schema``), every resource from the
resource registry, and the tag tree from the domain registry, so the grouping
is HQ's nav. A capability, resource or domain added anywhere appears here with
no edit.

Served at /api/v2/openapi.json with whatever extensions this deployment
composes. ``hq-api.openapi.json`` beside this module is the host alone,
committed for clients and code generation:

    python manage.py api_openapi          # rewrite hq-api.openapi.json
    python manage.py api_openapi --check  # exit 1 on drift

Examples are responses the suite really received, recorded into
``openapi-examples.json`` (see hq_api/testing.py) and validated against
this document on every run.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from http import HTTPStatus
from pathlib import Path
from typing import Any

from django.urls import URLPattern, URLResolver, get_resolver, reverse
from pydantic import TypeAdapter
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaMode, JsonSchemaValue
from pydantic_core import core_schema

from hq.platform.application.capabilities import capability_registry, describe_capabilities
from hq.platform.application.domains import Domain, all_domains
from hq.platform.application.resources import describe_resources, resource_registry

from . import urls, views

OPENAPI_VERSION = "3.2.0"
DOCUMENT_PATH = Path(__file__).with_name("hq-api.openapi.json")
EXAMPLES_PATH = Path(__file__).with_name("openapi-examples.json")
SCHEMAS = "#/components/schemas/"
# The tag for what belongs to the API itself rather than to one domain.
API_TAG = "api"
VERSION = views.CURRENT_API_VERSION
VERSION_TAG = f"api.v{VERSION}"
_PARAMETER = re.compile(r"<(?:\w+:)?(\w+)>")


class OpenAPIError(RuntimeError):
    """A route the document cannot describe; fails the build, not the client."""


class _Closed(GenerateJsonSchema):
    """A TypedDict's keys are all of its keys, as the strict API reads them."""

    def typed_dict_schema(self, schema: core_schema.TypedDictSchema) -> JsonSchemaValue:
        value = super().typed_dict_schema(schema)
        value.setdefault("additionalProperties", False)
        return value


def _pascal(name: str) -> str:
    return "".join(
        part[:1].upper() + part[1:] for part in re.split(r"[^0-9A-Za-z]+", name) if part
    )


def _camel(name: str) -> str:
    pascal = _pascal(name)
    return pascal[:1].lower() + pascal[1:]


def _first_line(doc: str | None) -> str:
    return (doc or "").strip().split("\n", 1)[0]


def routes() -> Iterator[tuple[str, URLPattern]]:
    """Every hq_api route with its full path, wherever the project mounts it."""

    ours = {id(pattern) for pattern in urls.urlpatterns}

    def walk(entries: list[Any], prefix: str) -> Iterator[tuple[str, URLPattern]]:
        for entry in entries:
            if isinstance(entry, URLResolver):
                yield from walk(entry.url_patterns, prefix + str(entry.pattern))
            elif isinstance(entry, URLPattern) and id(entry) in ours:
                yield prefix + str(entry.pattern), entry

    yield from walk(get_resolver().url_patterns, "/")


def _view(pattern: URLPattern) -> Any:
    """The view, with the attributes ``views._endpoint`` set on it."""

    return pattern.callback


def path_template(route: str) -> str:
    """A Django route as an OpenAPI path: ``<str:name>`` becomes ``{name}``."""

    return _PARAMETER.sub(r"{\1}", route)


class _Components:
    """components/schemas, filled once per name."""

    def __init__(self) -> None:
        self.schemas: dict[str, Any] = {}

    def typed(self, types: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Schemas for Python types, their definitions hoisted into components."""

        mode: JsonSchemaMode = "serialization"
        adapters = [(key, mode, TypeAdapter(kind)) for key, kind in types.items()]
        refs, definitions = TypeAdapter.json_schemas(
            adapters, ref_template=SCHEMAS + "{model}", schema_generator=_Closed
        )
        for name, schema in definitions.get("$defs", {}).items():
            self.add(name, schema)
        return {key: refs[(key, mode)] for key in types}

    def add(self, name: str, schema: dict[str, Any]) -> str:
        if self.schemas.setdefault(name, schema) != schema:
            raise OpenAPIError(f"Two schemas claim the component name {name!r}.")
        return SCHEMAS + name

    def hoist(self, schema: dict[str, Any], owner: str) -> dict[str, Any]:
        """Move a JSON Schema's local ``$defs`` into components.

        Inside an OpenAPI document ``#/$defs/X`` resolves against the document,
        not the schema, so local definitions are renamed under their owner and
        every reference rewritten.
        """

        definitions = schema.get("$defs", {})
        names = {key: f"{owner}{_pascal(key)}" for key in definitions}

        def rewrite(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    key: (
                        SCHEMAS + names[item.removeprefix("#/$defs/")]
                        if key == "$ref" and isinstance(item, str) and item.startswith("#/$defs/")
                        else rewrite(item)
                    )
                    for key, item in value.items()
                    if key != "$defs"
                }
            if isinstance(value, list):
                return [rewrite(item) for item in value]
            return value

        for key, definition in definitions.items():
            self.add(names[key], rewrite(definition))
        rewritten: dict[str, Any] = rewrite(schema)
        return rewritten


class _Tags:
    """The tag tree: nav groups, then domains, from the domain registry.

    A resource belongs to the domain that declares it (its integration or its
    records), else to the domain whose nav item is the resource's web page. A
    capability belongs to the domain that declares it, else to its subject
    resource's domain. What belongs to none is the API's own.
    """

    def __init__(self, resources: dict[str, Any], capabilities: dict[str, Any]) -> None:
        self.domains = {domain.id: domain for domain in all_domains()}
        self.resource_owner: dict[str, str] = {}
        self.capability_owner: dict[str, str] = {}
        pages: dict[str, str] = {}
        for domain in self.domains.values():
            integration = domain.integration
            for spec in integration.resources() if integration.resources else ():
                self.resource_owner.setdefault(spec.name, domain.id)
            if domain.records:
                self.resource_owner.setdefault(domain.records.resource, domain.id)
            for spec in integration.capabilities() if integration.capabilities else ():
                self.capability_owner.setdefault(spec.name, domain.id)
            for item in domain.navigation:
                pages.setdefault(item.route, domain.id)
        for name, resource in resources.items():
            if name not in self.resource_owner and resource.web_route in pages:
                self.resource_owner[name] = pages[resource.web_route]
        for name, capability in capabilities.items():
            owner = self.resource_owner.get(capability.subject_resource or "")
            if name not in self.capability_owner and owner:
                self.capability_owner[name] = owner
        self.used: set[str] = set()

    def of_resource(self, name: str) -> str:
        return self._use(self.resource_owner.get(name, API_TAG))

    def of_capability(self, name: str) -> str:
        return self._use(self.capability_owner.get(name, API_TAG))

    def _use(self, tag: str) -> str:
        self.used.add(tag)
        return tag

    @staticmethod
    def _group(domain: Domain) -> str:
        placed = sorted(domain.navigation, key=lambda item: (item.order, item.label))
        return placed[0].group if placed else ""

    def render(self) -> list[dict[str, Any]]:
        used = sorted(
            (self.domains[tag] for tag in self.used if tag in self.domains),
            key=lambda domain: (domain.bar_order, domain.label),
        )
        tags: list[dict[str, Any]] = [
            {
                "name": API_TAG,
                "summary": "Machine API",
                "description": _first_line(views.__doc__),
            }
        ]
        groups: list[str] = []
        for domain in used:
            group = self._group(domain)
            if group and group not in groups:
                groups.append(group)
        tags += [
            {"name": f"nav.{_camel(group)}", "summary": group, "kind": "nav"} for group in groups
        ]
        for domain in used:
            tag: dict[str, Any] = {"name": domain.id, "summary": domain.label, "kind": "nav"}
            if group := self._group(domain):
                tag["parent"] = f"nav.{_camel(group)}"
            tags.append(tag)
        tags.append({"name": VERSION_TAG, "summary": f"v{VERSION}", "kind": "badge"})
        return tags


def _json_content(schema: dict[str, Any], examples: dict[str, Any] | None = None) -> dict[str, Any]:
    media: dict[str, Any] = {"schema": schema}
    if examples:
        media["examples"] = examples
    return {"application/json": media}


def _recorded(value: Any) -> dict[str, Any]:
    return {"recorded": {"summary": "Recorded by HQ's test suite.", "dataValue": value}}


def _load_examples() -> dict[str, Any]:
    if not EXAMPLES_PATH.exists():
        return {}
    loaded: dict[str, Any] = json.loads(EXAMPLES_PATH.read_text(encoding="utf-8"))
    return loaded


class _Builder:
    def __init__(self) -> None:
        self.components = _Components()
        self.paths: dict[str, dict[str, Any]] = {}
        self.operation_ids: set[str] = set()
        self.examples = _load_examples()
        self.data = self.components.typed(
            {
                view.__name__: view.__hq_data__
                for _, pattern in routes()
                if (view := _view(pattern)).__hq_data__ is not None
            }
        )
        self.failure = self.components.typed({"Failure": views.Failure})["Failure"]
        self.capabilities = describe_capabilities()["capabilities"]
        self.resources = describe_resources()["resources"]
        self.resource_specs = resource_registry()
        self.tags = _Tags(self.resource_specs, capability_registry())

    def _responses(self, view: Any, operation_id: str) -> dict[str, Any]:
        recorded = self.examples.get(operation_id, {}).get("responses", {})
        if view.__hq_data__ is None:
            # This document itself.
            schema: dict[str, Any] = {
                "type": "object",
                "properties": {"openapi": {"type": "string", "pattern": r"^3\.2\.\d+$"}},
                "required": ["openapi", "info", "paths"],
            }
        else:
            schema = {
                "type": "object",
                "properties": {"ok": {"const": True}, "data": self.data.get(view.__name__, {})},
                "required": ["ok", "data"],
                "additionalProperties": False,
            }
        responses: dict[str, Any] = {
            "200": {
                "description": "Success.",
                "content": _json_content(schema, _recorded(recorded["200"]) if "200" in recorded else None),
            }
        }
        for status in view.__hq_errors__:
            code = str(status)
            if code in recorded:
                responses[code] = {
                    "description": f"{HTTPStatus(status).phrase}.",
                    "content": _json_content(self.failure, _recorded(recorded[code])),
                }
            else:
                responses[code] = {
                    "$ref": f"#/components/responses/{_pascal(HTTPStatus(status).phrase)}"
                }
        return responses

    def operation(
        self,
        path: str,
        method: str,
        view: Any,
        operation_id: str,
        tag: str,
        **fields: Any,
    ) -> None:
        operation_id += f"V{VERSION}"
        if operation_id in self.operation_ids:
            raise OpenAPIError(f"Operation id {operation_id!r} is derived twice.")
        self.operation_ids.add(operation_id)
        request = self.examples.get(operation_id, {}).get("request")
        if request is not None and "requestBody" in fields:
            for media in fields["requestBody"]["content"].values():
                media["examples"] = _recorded(request)
        operation: dict[str, Any] = {
            "operationId": operation_id,
            "summary": fields.pop("summary", None) or _first_line(view.__doc__),
            "tags": [tag, VERSION_TAG],
            **fields,
            "responses": self._responses(view, operation_id),
        }
        self.paths.setdefault(path, {})[method.lower()] = operation

    def route(self, route: str, pattern: URLPattern) -> None:
        view = _view(pattern)
        template = path_template(route)
        expand = EXPANSIONS.get(view.__name__)
        if _PARAMETER.search(route) and expand is None:
            raise OpenAPIError(f"No OpenAPI expansion for parameterised route {route!r}.")
        for method in view.__hq_methods__:
            if expand is not None:
                expand(self, template, method, view)
                continue
            query = [
                {"name": field, "in": "query", "required": False, "schema": {"type": "string"}}
                for field in getattr(view, "__hq_query_fields__", ())
            ]
            self.operation(
                template,
                method,
                view,
                _camel(view.__name__),
                self.tags._use(API_TAG),
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

    def capability_paths(self, template: str, method: str, view: Any) -> None:
        generic = views._request_schema({"input_schema": {"type": "object"}, "target": None})
        generic["properties"]["target"] = {"type": ["string", "integer"]}
        self.operation(
            template,
            method,
            view,
            _camel(view.__name__),
            self.tags._use(API_TAG),
            parameters=[_path_parameter("name", {"type": "string"}, "A capability name.")],
            requestBody=_body(generic),
        )
        for spec in self.capabilities:
            name = spec["name"]
            body = {
                "$ref": self.components.add(
                    f"{_pascal(name)}Request",
                    self.components.hoist(views._request_schema(spec), _pascal(name)),
                )
            }
            grants = ", ".join(spec["required_capabilities"]) or "none"
            self.operation(
                template.replace("{name}", name),
                method,
                view,
                _camel(name),
                self.tags.of_capability(name),
                summary=spec["label"],
                description=f"{spec['summary']}\n\nRequires: {grants}.",
                parameters=(
                    [{"$ref": "#/components/parameters/IdempotencyKey"}] if spec["effect"] != "read" else []
                ),
                requestBody=_body(body),
                **{
                    "x-hq-capability": name,
                    "x-hq-effect": spec["effect"],
                    "x-hq-required-capabilities": spec["required_capabilities"],
                },
            )

    def resource_list_paths(self, template: str, method: str, view: Any) -> None:
        self.operation(
            template,
            method,
            view,
            _camel(view.__name__),
            self.tags._use(API_TAG),
            parameters=[_path_parameter("name", {"type": "string"}, "A resource name.")],
        )
        for spec in self.resources:
            listing = spec["operations"]["list"]
            if listing is None:
                continue
            name = spec["name"]
            query = self.components.hoist(listing["query_schema"], f"{_pascal(name)}Query")
            self.operation(
                template.replace("{name}", name),
                method,
                view,
                f"list{_pascal(name)}",
                self.tags.of_resource(name),
                summary=f"List {spec['label']}",
                description=spec["summary"],
                # Emit usable client filters and retain the whole strict query contract.
                parameters=[
                    {
                        "name": field,
                        "in": "query",
                        "required": field in query.get("required", []),
                        "style": "form",
                        "explode": True,
                        "schema": schema,
                        **({"description": schema["description"]} if "description" in schema else {}),
                    }
                    for field, schema in query.get("properties", {}).items()
                ],
                **{"x-hq-resource": name, "x-hq-query-schema": query},
            )

    def resource_detail_paths(self, template: str, method: str, view: Any) -> None:
        self.operation(
            template,
            method,
            view,
            _camel(view.__name__),
            self.tags._use(API_TAG),
            parameters=[
                _path_parameter("name", {"type": "string"}, "A resource name."),
                _path_parameter("identifier", {"type": "string"}, "The record's identifier."),
            ],
        )
        for spec in self.resources:
            detail = spec["operations"]["get"]
            if detail is None:
                continue
            name = spec["name"]
            identifier = TypeAdapter(self.resource_specs[name].identifier_type).json_schema()
            self.operation(
                template.replace("{name}", name),
                method,
                view,
                f"get{_pascal(name)}",
                self.tags.of_resource(name),
                summary=f"Get one of {spec['label']}",
                description=spec["summary"],
                parameters=[
                    _path_parameter("identifier", identifier, f"The {detail['identifier']}.")
                ],
                **{"x-hq-resource": name, "x-hq-identifier": detail["identifier"]},
            )


EXPANSIONS: dict[str, Callable[[_Builder, str, str, Any], None]] = {
    views.execute.__name__: _Builder.capability_paths,
    views.resource_list.__name__: _Builder.resource_list_paths,
    views.resource_detail.__name__: _Builder.resource_detail_paths,
}


def _path_parameter(name: str, schema: dict[str, Any], description: str) -> dict[str, Any]:
    return {
        "name": name,
        "in": "path",
        "required": True,
        "description": description,
        "schema": schema,
    }


def _body(schema: dict[str, Any]) -> dict[str, Any]:
    # Optional: an empty body is an empty command.
    return {"required": False, "content": _json_content(schema)}


def document() -> dict[str, Any]:
    """The OpenAPI 3.2 document for this deployment's machine API."""

    builder = _Builder()
    for route, pattern in routes():
        builder.route(route, pattern)
    statuses = sorted(
        {status for _, pattern in routes() for status in _view(pattern).__hq_errors__}
    )
    responses: dict[str, Any] = {}
    for status in statuses:
        phrase = HTTPStatus(status).phrase
        response: dict[str, Any] = {
            "description": f"{phrase}.",
            "content": _json_content(builder.failure),
        }
        if status == 401:
            response["headers"] = {
                "WWW-Authenticate": {
                    "description": "How to authenticate.",
                    "schema": {"type": "string", "const": views.REALM},
                }
            }
        responses[_pascal(phrase)] = response
    value: dict[str, Any] = {
        "openapi": OPENAPI_VERSION,
        "$self": reverse("hq_api:openapi"),
        "jsonSchemaDialect": "https://json-schema.org/draft/2020-12/schema",
        "info": {
            "title": "Severino HQ machine API",
            "version": str(VERSION),
            "description": _first_line(views.__doc__)
            + " Derived from HQ's routes and its capability, resource and domain registries.",
        },
        "security": [{"bearer": []}],
        "tags": builder.tags.render(),
        "paths": dict(sorted(builder.paths.items())),
        "components": {
            "securitySchemes": {
                "bearer": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "JWT",
                    "description": "An access token from HQ's identity provider, addressed to this API.",
                }
            },
            "parameters": {
                "IdempotencyKey": {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "description": "Replays the committed response when the same request is retried.",
                    "schema": {"type": "string", "minLength": 1},
                }
            },
            "responses": responses,
            "schemas": dict(sorted(builder.components.schemas.items())),
        },
    }
    from hq.platform.mcp.declarations import tool_contract

    value["x-hq-mcp-tools"] = tool_contract(value)
    return value


def render(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"
