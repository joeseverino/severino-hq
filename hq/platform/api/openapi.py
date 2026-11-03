"""HQ's machine API as an OpenAPI 3.2 document, derived and never hand-written.

Served at /api/v2/openapi.json with whatever extensions this deployment
composes; ``hq-api.openapi.json`` beside this module is the host alone,
committed for clients. Each path is a route of the API's URLconf, its
operations and answers its view's ``_endpoint`` declaration, each capability
and resource its registry's entry, and the tag tree the domain registry's.
"""

import json
import re
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from http import HTTPStatus
from pathlib import Path
from typing import Any, TypeAliasType, get_args

from django.urls import URLPattern, reverse

from hq.platform.application.capabilities import capability_registry, describe_capabilities
from hq.platform.application.contracts import effect_glossary
from hq.platform.application.domains import NAVIGATION_GROUPS, Domain, all_domains, domain_navigation
from hq.platform.application.integration_specs import (
    FIELD_MASK,
    IDEMPOTENCY_FIELD,
    IDEMPOTENCY_FIELD_SCHEMA,
    IDEMPOTENCY_HEADER,
    RESERVED_QUERY_FIELD,
    CapabilitySpec,
)
from hq.platform.application.json_schemas import LOCAL, json_schema, json_schemas
from hq.platform.application.plugins import NavigationItem
from hq.platform.application.refusal_status import LATER_CODES
from hq.platform.application.resources import describe_resources, resource_registry
from hq.platform.application.security import API_INTERFACE, Principal
lazy from hq.platform.application.routes import routed

from . import urls, views
from .fragments import OpenAPIError, pascal as _pascal
from .refusal_names import LATER_STATUSES, codes_by_status, refusal_name

OPENAPI_VERSION = "3.2.0"
DOCUMENT_PATH = Path(__file__).with_name("hq-api.openapi.json")
EXAMPLES_PATH = Path(__file__).with_name("openapi-examples.json")
SCHEMAS = "#/components/schemas/"
PARAMETERS = "#/components/parameters/"
RESPONSES = "#/components/responses/"
EXAMPLES = "#/components/examples/"
HEADERS = "#/components/headers/"
# The tag for what belongs to the API itself rather than to one domain: the
# domain whose page this document is drawn on, so it stands where the bar has it.
API_TAG = "hq.api"
# What a nav group's own tag is named under, where no domain heads the group.
_GROUP_PREFIX = "nav."
VERSION = views.CURRENT_API_VERSION
_PARAMETER = re.compile(r"<(?:\w+:)?(\w+)>")


def _camel(name: str) -> str:
    pascal = _pascal(name)
    return pascal[:1].lower() + pascal[1:]


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


class _Components:
    """components/schemas, filled once per name."""

    def __init__(self) -> None:
        self.schemas: dict[str, Any] = {}
        # The components a caller's field mask may trim, which promise no field.
        self.masked: set[str] = set()

    def mask(self, name: str) -> None:
        """Promise no field of the component ``name``, whichever schema writes it first."""

        self.masked.add(name)
        if name in self.schemas:
            self.schemas[name].pop("required", None)

    def typed(self, types: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Schemas for Python types, their definitions hoisted into components."""

        refs, definitions = json_schemas(types, mode="serialization", ref_template=SCHEMAS + "{model}")
        for name, schema in definitions.items():
            self.add(name, schema)
        return refs

    def add(self, name: str, schema: dict[str, Any]) -> str:
        if name in self.masked:
            schema = {key: value for key, value in schema.items() if key != "required"}
        if self.schemas.setdefault(name, schema) != schema:
            raise OpenAPIError(
                f"Two schemas claim the component name {name!r}: each meaning is a type alias of its own name."
            )
        return SCHEMAS + name

    def hoist(self, schema: dict[str, Any]) -> dict[str, Any]:
        """Move a JSON Schema's local ``$defs`` into components, each under its own name.

        Inside an OpenAPI document ``#/$defs/X`` resolves against the document,
        not the schema. A definition is named for the type it is, so one alias
        used by many schemas is one component, and two that mean different
        things under one name fail in ``add``.
        """

        for key, definition in schema.get("$defs", {}).items():
            self.add(key, _rewritten(definition))
        rewritten: dict[str, Any] = _rewritten(schema)
        return rewritten


def _rewritten(value: Any) -> Any:
    """``value`` with each local ``$ref`` pointing at the component of the same name, its ``$defs`` gone."""

    if isinstance(value, dict):
        return {
            key: (
                SCHEMAS + item.removeprefix(LOCAL)
                if key == "$ref" and isinstance(item, str) and item.startswith(LOCAL)
                else _rewritten(item)
            )
            for key, item in value.items()
            if key != "$defs"
        }
    if isinstance(value, list):
        return [_rewritten(item) for item in value]
    return value


def _declared_resources(domain: Domain) -> list[str]:
    """The resources a domain declares: its integration's, then its records'."""

    integration = domain.integration
    names = [spec.name for spec in integration.resources()] if integration.resources else []
    return [*names, domain.records.resource] if domain.records else names


def _declared_capabilities(domain: Domain) -> list[str]:
    """The capabilities a domain declares: its integration's commands, and its
    outbound work, each piece asked for through the capability of its name."""

    integration = domain.integration
    commands = [spec.name for spec in integration.capabilities()] if integration.capabilities else []
    return [*commands, *(work.name for work in integration.outbound())] if integration.outbound else commands


def _bar(item: NavigationItem) -> tuple[int, str]:
    return item.order, item.label


def _first_declarer(domains: Iterable[Domain], declared: Callable[[Domain], list[str]]) -> dict[str, str]:
    """Each name some domain declares, owned by the first domain to declare it."""

    owner: dict[str, str] = {}
    for domain in domains:
        for name in declared(domain):
            owner.setdefault(name, domain.id)
    return owner


class _Tags:
    """The document's sections: the domains of the domain registry under their nav groups, in the bar's order."""

    def __init__(self, resources: dict[str, Any], capabilities: dict[str, Any]) -> None:
        self.domains = {domain.id: domain for domain in all_domains()}
        domains = self.domains.values()
        # What a domain serves stands in its section by one rule, the host's
        # core and every extension alike: the domain that declares it, else the
        # domain whose nav item is its page (a resource's web page, a
        # capability's starting page, a route's `page`), else, for a
        # capability, the domain of the resource it acts on. What none places
        # carries no tag, which test_sections refuses; only the API's own
        # routes, which serve every domain, stand in its section.
        self.pages = _first_declarer(domains, lambda domain: [item.route for item in domain.navigation])
        self.resource_owner = _first_declarer(domains, _declared_resources)
        for name, resource in resources.items():
            if name not in self.resource_owner and resource.web_route in self.pages:
                self.resource_owner[name] = self.pages[resource.web_route]
        declared = _first_declarer(domains, _declared_capabilities)
        self.capability_owner = {
            name: owner
            for name, capability in capabilities.items()
            if (
                owner := declared.get(name)
                or self.pages.get(capability.reached_from)
                or self.resource_owner.get(capability.subject_resource or "")
            )
        }
        self.placed = {item.route: item for item in reversed(domain_navigation())}
        self.used: set[str] = set()

    def of_resource(self, name: str) -> str | None:
        return self.use(self.resource_owner.get(name))

    def of_capability(self, name: str) -> str | None:
        return self.use(self.capability_owner.get(name))

    def of_route(self, page: str) -> str | None:
        """A route's section: the domain of the page it serves, or the API's own for a route serving every domain."""

        return self.use(self.pages.get(page) if page else API_TAG)

    def use(self, tag: str | None) -> str | None:
        if tag:
            self.used.add(tag)
        return tag

    def _item(self, domain: Domain) -> NavigationItem | None:
        """A domain's first nav item where the bar draws it: an extension may
        place a host page in a group of its own (``domain_navigation``)."""

        placed = sorted((self.placed.get(item.route, item) for item in domain.navigation), key=_bar)
        return placed[0] if placed else None

    def _group(self, domain: Domain) -> str:
        item = self._item(domain)
        return item.group if item else ""

    def _place(self) -> Callable[[Domain], tuple[bool, bool, int, str]]:
        """Where a domain stands: the API's own domain is where a client starts,
        so it and its group lead; the rest keep the bar's order."""

        api = self.domains.get(API_TAG)
        leading = self._group(api) if api else ""

        def place(domain: Domain) -> tuple[bool, bool, int, str]:
            first = domain.id == API_TAG or (bool(leading) and self._group(domain) == leading)
            item = self._item(domain)
            return (not first, domain.id != API_TAG, item.order if item else domain.bar_order, domain.label)

        return place

    def _groups(self, place: Callable[[Domain], Any]) -> dict[str, dict[str, Any]]:
        """Each nav group's tag, by the group's name.

        A group says what it is for as the host or the extension that adds it
        declares. A domain named as its group is that group's section, so no
        section reads "Projects > Projects": it heads the group, its siblings
        under it.
        """

        purposes = {group.name: group.description for domain in self.domains.values() for group in domain.groups}
        purposes.update(NAVIGATION_GROUPS)
        groups: dict[str, dict[str, Any]] = {}
        for domain in sorted(self.domains.values(), key=place):
            group = self._group(domain)
            heads = domain.label == group
            held = groups.get(group)
            # The first domain named as the group heads it; until one does, the group is its own tag.
            if not group or (held is not None and not (heads and held["name"].startswith(_GROUP_PREFIX))):
                continue
            tag = {"name": domain.id if heads else _GROUP_PREFIX + _camel(group), "summary": group, "kind": "nav"}
            if said := purposes.get(group) or (domain.description if heads else ""):
                tag["description"] = said
            groups[group] = tag
        return groups

    def render(self) -> list[dict[str, Any]]:
        place = self._place()
        groups = self._groups(place)
        # A group stands where its first domain does, and a domain outside
        # every group keeps its own place among them.
        tags: list[dict[str, Any]] = []
        for domain in sorted((self.domains[tag] for tag in self.used if tag in self.domains), key=place):
            group = groups.get(self._group(domain))
            if group is not None and group not in tags:
                tags.append(group)
            if group is not None and group["name"] == domain.id:
                continue
            tag: dict[str, Any] = {"name": domain.id, "summary": domain.label, "kind": "nav"}
            if domain.description:
                tag["description"] = domain.description
            if group is not None:
                tag["parent"] = group["name"]
            tags.append(tag)
        return tags


def _json_content(schema: dict[str, Any], examples: dict[str, Any] | None = None) -> dict[str, Any]:
    media: dict[str, Any] = {"schema": schema}
    if examples:
        media["examples"] = examples
    return {"application/json": media}


def _load_examples() -> dict[str, Any]:
    if not EXAMPLES_PATH.exists():
        return {}
    loaded: dict[str, Any] = json.loads(EXAMPLES_PATH.read_text(encoding="utf-8"))
    return loaded


def prune_examples(value: dict[str, Any]) -> list[str]:
    """Drop each recorded example whose operation ``value`` no longer has, and say which.

    A recording run reads the examples file before it writes the new one, so
    an entry whose operation moved would fail that run's conformance check.
    """

    operations = {operation["operationId"] for methods in value["paths"].values() for operation in methods.values()}
    examples = _load_examples()
    gone = sorted(set(examples) - operations)
    if gone:
        EXAMPLES_PATH.write_text(render({key: entry for key, entry in examples.items() if key in operations}))
    return gone


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
        # The envelopes each answer's schema is drawn from.
        self.components.typed({"Failure": views.Failure, "Success": views.Success})
        self.responses: dict[str, Any] = {}
        self.parameters: dict[str, Any] = {}
        self.headers: dict[str, Any] = {}
        self.recordings: dict[str, Any] = {}
        # Each request body's first name, by what it says.
        self.requests: dict[str, str] = {}
        # What a token could ever run: /api/v2 is called with tokens.
        self.capabilities = describe_capabilities(principal=Principal("openapi", API_INTERFACE, frozenset()))[
            "capabilities"
        ]
        self.resources = [describe_resources(name=name)["resources"][0] for name in resource_registry()]
        self.resource_specs = resource_registry()
        self.tags = _Tags(self.resource_specs, capability_registry())

    def recorded(self, name: str, value: Any) -> dict[str, Any]:
        """The examples of a part ``value`` was recorded for, kept as the component ``name``.

        Each recording is its operation's own, so two operations recorded
        alike still answer apart.
        """

        self.recordings[name] = {"summary": "Recorded by HQ's test suite.", "dataValue": value}
        return {"recorded": {"$ref": EXAMPLES + name}}

    def _responses(
        self, view: Any, operation_id: str, codes: Iterable[str], answer: tuple[str, dict[str, Any]] | None
    ) -> dict[str, Any]:
        recorded = self.examples.get(operation_id, {}).get("responses", {})
        schema: dict[str, Any]
        if answer is not None:
            envelope = self.components.schemas["Success"]
            schema = {**envelope, "properties": {**envelope["properties"], "data": answer[1]}}
        elif view.__hq_data__ is None:
            # This document itself.
            schema = {
                "type": "object",
                "properties": {"openapi": {"type": "string", "pattern": r"^3\.2\.\d+$"}},
                "required": ["openapi", "info", "paths"],
            }
        else:
            envelope = self.components.schemas["Success"]
            schema = {**envelope, "properties": {**envelope["properties"], "data": self.data.get(view.__name__, {})}}
        success: dict[str, Any] = {"description": "Success."}
        if "200" in recorded:
            success["content"] = _json_content(schema, self.recorded(f"{operation_id}OK", recorded["200"]))
        else:
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
            success = self.component("responses", self.responses, name, {**success, "content": _json_content(schema)})
        responses: dict[str, Any] = {"200": success}
        for status, answered in codes_by_status(codes).items():
            name = self.refusal(status, answered)
            if str(status) in recorded:
                example = self.recorded(f"{operation_id}{_pascal(HTTPStatus(status).phrase)}", recorded[str(status)])
                responses[str(status)] = {
                    **self.responses[name],
                    "content": _json_content({"$ref": f"{SCHEMAS}{name}Refusal"}, example),
                }
            else:
                responses[str(status)] = {"$ref": RESPONSES + name}
        return responses

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
        response: dict[str, Any] = {
            "description": f"{HTTPStatus(status).phrase}.",
            "content": _json_content({"$ref": SCHEMAS + schema}),
        }
        if status == HTTPStatus.UNAUTHORIZED:
            response["headers"] = self.header(
                name,
                "WWW-Authenticate",
                {"description": "How to authenticate.", "schema": {"type": "string", "const": views.REALM}},
            )
        if later:
            response["headers"] = self.header(
                name,
                "Retry-After",
                {
                    "description": "Whole seconds until asking again can succeed, as `error.retry_after` says.",
                    "required": waits,
                    "schema": {"type": "integer", "minimum": 1},
                },
            )
        if self.responses.setdefault(name, response) != response:
            raise OpenAPIError(f"Two responses claim the component name {name!r}.")
        return name

    @staticmethod
    def component(section: str, held: dict[str, Any], name: str, value: dict[str, Any]) -> dict[str, Any]:
        """``value``, kept as the component ``name`` of ``section``, and the ``$ref`` that uses it."""

        if held.setdefault(name, value) != value:
            raise OpenAPIError(f"Two {section} claim the component name {name!r}.")
        return {"$ref": f"#/components/{section}/{name}"}

    def parameter(self, name: str, value: dict[str, Any]) -> dict[str, Any]:
        return self.component("parameters", self.parameters, name, value)

    def header(self, response: str, header: str, value: dict[str, Any]) -> dict[str, Any]:
        """``header`` of the response component ``response``, kept as a component of its own,
        since a recorded copy of the response sends it too."""

        name = response + _pascal(header)
        if self.headers.setdefault(name, value) != value:
            raise OpenAPIError(f"Two headers claim the component name {name!r}.")
        return {header: {"$ref": HEADERS + name}}

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
        # and answer a narrower type: its name and the schema of its data.
        codes = fields.pop("codes", view.__hq_codes__)
        answer = fields.pop("answer", None)
        operation_id += f"V{VERSION}"
        if operation_id in self.operation_ids:
            raise OpenAPIError(f"Operation id {operation_id!r} is derived twice.")
        self.operation_ids.add(operation_id)
        request = self.examples.get(operation_id, {}).get("request")
        if request is not None and "requestBody" in fields:
            for media in fields["requestBody"]["content"].values():
                media["examples"] = self.recorded(f"{operation_id}Request", request)
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
            "responses": self._responses(view, operation_id, codes, answer),
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
                _query_parameter(field, schema) for field, schema in getattr(view, "__hq_query_fields__", {}).items()
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

    def retry_key(self, request: dict[str, Any]) -> dict[str, Any]:
        """``request`` with its payload's retry key the one shared schema the header's is."""

        payload = request["properties"]["payload"]
        declared = payload.get("properties", {}).get(IDEMPOTENCY_FIELD)
        if declared is None:
            return request
        if declared != IDEMPOTENCY_FIELD_SCHEMA:
            raise OpenAPIError(f"A payload's {IDEMPOTENCY_FIELD} is not the retry key's one shape.")
        shared = {"$ref": self.components.add(IDEMPOTENCY_KEY, dict(IDEMPOTENCY_FIELD_SCHEMA))}
        properties = {**payload["properties"], IDEMPOTENCY_FIELD: shared}
        return {**request, "properties": {**request["properties"], "payload": {**payload, "properties": properties}}}

    # Expansions: one templated operation for the route itself, then one
    # concrete path per registry entry it serves. OpenAPI matches a concrete
    # path before its template.

    def capability_paths(self, template: str, method: str, view: Any) -> None:
        generic = self.components.hoist(views.request_schema({"input_schema": {"type": "object"}, "target": None}))
        generic["properties"]["target"] = views.ENVELOPE_SCHEMA["properties"]["target"]
        self.operation(
            template,
            method,
            view,
            _camel(view.__name__),
            self.tags.use(API_TAG),
            parameters=[_named("capability", [spec["name"] for spec in self.capabilities], "call")],
            requestBody=_body(generic),
        )
        registry = capability_registry()
        requests = {
            spec["name"]: self.retry_key(self.components.hoist(views.request_schema(spec)))
            for spec in self.capabilities
        }
        # A target several capabilities take in the same words is written once,
        # as the component named for the first of them.
        targets: dict[str, list[str]] = {}
        for name, request in requests.items():
            if "target" in request["properties"]:
                targets.setdefault(json.dumps(request["properties"]["target"], sort_keys=True), []).append(name)
        for said, takers in targets.items():
            if len(takers) > 1:
                shared = {"$ref": self.components.add(f"{_pascal(takers[0])}Target", json.loads(said))}
                for name in takers:
                    requests[name] = {
                        **requests[name],
                        "properties": {**requests[name]["properties"], "target": shared},
                    }
        # A command several capabilities take in one shape is the component
        # named for its type; one taken in a shape of its own (an update's
        # shows no defaults) stays in its body.
        shapes: dict[str, Counter[str]] = {}
        for name, request in requests.items():
            said = json.dumps(request["properties"]["payload"], sort_keys=True)
            shapes.setdefault(registry[name].command_type.__name__, Counter())[said] += 1
        for spec in self.capabilities:
            name = spec["name"]
            request = requests[name]
            command = registry[name].command_type.__name__
            payload = request["properties"]["payload"]
            if shapes[command][json.dumps(payload, sort_keys=True)] > 1:
                shared = {"$ref": self.components.add(command, payload)}
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
                summary=spec["label"],
                description=spec["summary"],
                parameters=([{"$ref": "#/components/parameters/IdempotencyKey"}] if spec["effect"] != "read" else []),
                requestBody=_body(body),
                codes=views.execute_codes(registry[name]),
                answer=self.answer_of(registry[name]),
                **{
                    "x-hq-capability": name,
                    EFFECT_EXTENSION: spec["effect"],
                    "x-hq-required-capabilities": spec["required_capabilities"],
                },
            )

    def answer_of(self, spec: CapabilitySpec) -> tuple[str, dict[str, Any]] | None:
        """A capability's answer where its handler names one, under the name of its result type.

        None leaves the view's object, for a handler whose result is a plain mapping.
        """

        data = views.capability_data(spec)
        if data is None:
            return None
        kinds = [kind for kind in get_args(data) if kind is not views.AwaitingApproval]
        name = "".join(kind.__name__ for kind in kinds) or data.__name__
        schema = self.components.typed({spec.name: data})[spec.name]
        return (name if spec.effect == "read" else f"{name}OrHeld"), schema

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
            query = listing["query_schema"]
            required = query.get("required", [])
            self.operation(
                template.replace("{name}", name),
                method,
                view,
                f"list{_pascal(name)}",
                self.tags.of_resource(name),
                # Titled as a read of one is, so it never reads as its section's
                # name or as a bare verb.
                summary=f"{spec['label']}: the list",
                description=spec["summary"],
                # Emit usable client filters and retain the whole strict query contract.
                parameters=[
                    self.filter(query, field, required=field in required) for field in query.get("properties", {})
                ]
                + [_FIELDS],
                answer=self.page_of(self.resource_specs[name].row_type),
                **{"x-hq-resource": name},
            )

    def typed(self, kind: type) -> dict[str, Any]:
        """The ``$ref`` to a resource's row or record, kept as the component of its name.

        A caller's field mask may keep any of its fields, so the component
        names each field's type and promises none of them.
        """

        name = kind.__name__
        if name not in self.components.schemas:
            self.components.typed({name: kind})
        self.components.mask(name)
        return {"$ref": SCHEMAS + name}

    def page_of(self, row: type | None) -> tuple[str, dict[str, Any]] | None:
        """A list's answer whose items are ``row``: every list's page, its items narrowed to it."""

        if row is None:
            return None
        page = self.data[views.resource_list.__name__]
        items = {"type": "array", "items": self.typed(row)}
        return f"{row.__name__}Page", {"allOf": [page], "properties": {"items": items}}

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

    def resource_detail_paths(self, template: str, method: str, view: Any) -> None:
        self.operation(
            template,
            method,
            view,
            _camel(view.__name__),
            self.tags.use(API_TAG),
            parameters=[
                _named(
                    "resource",
                    [spec["name"] for spec in self.resources if spec["operations"]["get"]],
                    "read of one record",
                ),
                _path_parameter(
                    "identifier", {"type": "string"}, "The record's value of the field its resource is addressed by."
                ),
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
                f"get{_pascal(name)}",
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
                    _FIELDS,
                ],
                answer=self.record_of(self.resource_specs[name].record_type),
                **{"x-hq-resource": name, "x-hq-identifier": field},
            )


_FIELDS = {"$ref": "#/components/parameters/Fields"}
IDEMPOTENCY_KEY = "IdempotencyKey"
# Where a capability operation says its effect, which the document's introduction explains.
EFFECT_EXTENSION = "x-hq-effect"


EXPANSIONS: dict[str, Callable[[_Builder, str, str, Any], None]] = {
    views.execute.__name__: _Builder.capability_paths,
    views.resource_list.__name__: _Builder.resource_list_paths,
    views.resource_detail.__name__: _Builder.resource_detail_paths,
}


def _query_parameter(name: str, schema: Mapping[str, Any], *, required: bool = False, **style: Any) -> dict[str, Any]:
    """A query parameter for one field of a query's schema, its words on the parameter rather than its schema."""

    parameter: dict[str, Any] = {
        "name": name,
        "in": "query",
        "required": required,
        **style,
        "schema": {key: value for key, value in schema.items() if key != "description"},
    }
    if "description" in schema:
        parameter["description"] = schema["description"]
    return parameter


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
            "parameters": dict(
                sorted(
                    {
                        "IdempotencyKey": {
                            "name": IDEMPOTENCY_HEADER,
                            "in": "header",
                            "required": False,
                            "schema": {"$ref": builder.components.add(IDEMPOTENCY_KEY, dict(IDEMPOTENCY_FIELD_SCHEMA))},
                        },
                        **builder.parameters,
                        "Fields": {
                            "name": RESERVED_QUERY_FIELD,
                            "in": "query",
                            "required": False,
                            "description": f"{FIELD_MASK} Sent as one comma-separated value (`observed_at,items.waiting`).",
                            "style": "form",
                            "explode": False,
                            "schema": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                        },
                    }.items()
                )
            ),
            "responses": dict(sorted(builder.responses.items())),
            "headers": dict(sorted(builder.headers.items())),
            "examples": dict(sorted(builder.recordings.items())),
            "schemas": dict(sorted(builder.components.schemas.items())),
        },
    }


def render(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"
