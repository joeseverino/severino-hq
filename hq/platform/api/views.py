"""Versioned machine-client transport over the shared capability registry.

It adds no capability, domain model or business rule, so the web UI, the CLI,
MCP and a Shortcut cannot drift into four behaviors. The version is in the
path, not a header, so a Shortcut not opened in months never meets a URL that
quietly changed meaning.
"""

import json
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Annotated, Any, Concatenate, TypedDict, cast

from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser
from django.core.exceptions import RequestDataTooBig
from django.http import HttpRequest, HttpResponse
from django.urls import URLPattern, reverse
from django.views.decorators.csrf import csrf_exempt
from pydantic import (
    BaseModel,
    Field,
    GetPydanticSchema,
    ValidationError as PydanticValidationError,
)

from hq.platform.application import agent_admission
from hq.platform.application.agent_access import AgentRefusal
from hq.platform.application.agent_limits import Limited
from hq.platform.application.answer_fields import Outcome, Pinned
from hq.platform.application.call_envelope import ENVELOPE_SCHEMA, INTEGER_TARGET, read_body
from hq.platform.application.capabilities import (
    CapabilitiesVersion,
    CapabilityDescription,
    describe_capabilities,
    key_required,
    run_capability,
)
from hq.platform.application.connections import ConnectionCatalog, describe_connections
from hq.platform.application.describe import Describe
from hq.platform.application.findings import Findings, FindingsQuery, findings as application_findings
from hq.platform.application.held_answer import AwaitingApproval
from hq.platform.application.integration_specs import (
    CURRENT_API_VERSION,
    IDEMPOTENCY_HEADER,
    RESERVED_QUERY_FIELD,
    CapabilitySpec,
)
from hq.platform.application.json_schemas import json_schema
from hq.platform.application.pages import page_refusal
from hq.platform.application.projection import QueryRefusal
from hq.platform.application.refusal_status import (
    Door,
    Refusal,
    RefusalEnvelope,
    answered_by,
    capability_doors,
    refusal_of,
)
from hq.platform.application.request_identity import signed_in
from hq.platform.application.resources import (
    ResourceCatalog,
    ResourceCollection,
    describe_resources,
    get_resource as get_application_resource,
    list_resource as list_application_resource,
)
from hq.platform.application.security import API_INTERFACE, Principal, web_principal
from hq.platform.application.tokens import is_configured
from hq.platform.application.topology import TopologyQuery, topology as application_topology
from hq.platform.application.topology_answer import TopologyAnswer
from hq.platform.core import strict_json
from hq.platform.core.network import client_ip

lazy from . import urls
lazy from .openapi import document

REALM = 'Bearer realm="Severino HQ"'


class APIRequest(HttpRequest):
    """A request `_endpoint` has authenticated: who is calling, and with what."""

    principal: Principal


View = Callable[Concatenate[HttpRequest, ...], HttpResponse]
APIView = Callable[Concatenate[APIRequest, ...], HttpResponse]


# What these views answer, typed once: the OpenAPI document (hq_api/openapi.py)
# derives its schemas from these, and its contract tests hold real responses
# to them.
# The refusal envelope, under the name the document's schema carries.
class Failure(RefusalEnvelope):
    pass


class Ok[T](TypedDict):
    """Every success: ``ok``, and what the operation answers under ``data``."""

    ok: Annotated[Outcome, Pinned(value=True)]
    data: Annotated[T, Field(description="What the operation answers, in the shape its response names.")]


# The success envelope, under the name the document's schema carries; each
# operation's draws its ``data`` from the view's declaration.
class Success(Ok[Any]):
    pass


def first_line(doc: str | None) -> str:
    """A docstring's summary: what its operation's description opens with."""

    return (doc or "").strip().split("\n", 1)[0]


def _linked() -> dict[str, URLPattern]:
    """The API's own reads the root links: each named route of the URLconf that takes no parameter, but the root."""

    return {
        pattern.name: pattern
        for pattern in urls.urlpatterns
        if pattern.name and pattern.name != "root" and not pattern.pattern.converters
    }


def root_links() -> Any:
    """``RootLinks``: one path for each read ``_linked`` finds, said in its view's summary."""

    # Called through Any: the keys are the URLconf's, which a checker cannot read.
    links = cast(Any, TypedDict)(
        "RootLinks",
        {
            name: Annotated[str, Field(description=first_line(pattern.callback.__doc__))]
            for name, pattern in _linked().items()
        },
    )
    links.__doc__ = "Where each of the API's own reads is, every one a path on this host."
    return links


class RootData(TypedDict):
    """What this API is, and what the presented credential may do."""

    service: Annotated[str, Describe("Which service this is: always `severino-hq`.")]
    api_version: Annotated[
        int, Describe("The version of the API's contract this host serves, the number after `v` in each path.")
    ]
    resource: Annotated[str, Describe("The audience a token must be issued for to be accepted here.")]
    actor: Annotated[str, Describe("Who the presented credential acts as, by the name HQ records its calls under.")]
    granted: Annotated[list[str], Describe("The permissions the credential holds, sorted.")]
    # Typed by the URLconf, read once this module and it are both loaded.
    links: Annotated[dict[str, str], GetPydanticSchema(lambda _source, handler: handler(root_links()))]


class CapabilityEntry(CapabilityDescription):
    """One capability as the machine API describes it: with the body it is called by."""

    idempotency_key_required: Annotated[
        bool,
        Describe(
            "Whether this caller must send an idempotency key to run it: any change, from all but the host's shell."
        ),
    ]
    request_schema: Annotated[dict[str, Any], Describe("The JSON Schema of the body a call to run it sends.")]


class CapabilityRequestCatalog(TypedDict):
    """The capability catalog as the machine API answers it: each entry with the body it is called by."""

    schema_version: CapabilitiesVersion
    capabilities: Annotated[
        list[CapabilityEntry], Describe("Each capability asked for that this caller's interface can run.")
    ]


def _json(payload: Mapping[str, Any], *, status: int = 200) -> HttpResponse:
    response = HttpResponse(
        json.dumps(payload, separators=(",", ":"), sort_keys=True),
        content_type="application/json",
        status=status,
    )
    # Operational data reached with a bearer credential: correct nowhere but
    # the client that asked for it.
    response["Cache-Control"] = "private, no-store"
    return response


def _ok(data: Any, *, status: int = 200) -> HttpResponse:
    answer: Ok[Any] = {"ok": True, "data": data}
    return _json(answer, status=status)


def _answer(refusal: Refusal) -> HttpResponse:
    """A refusal, at the status its code has everywhere (``refusal_status``)."""

    response = _json(refusal.answer(), status=refusal.status)
    if refusal.status == 401:
        # A native client cannot use an HTML login page. Saying *how* to
        # authenticate is the difference between a retryable failure and a
        # Shortcut that silently shows a wall of markup.
        response["WWW-Authenticate"] = REALM
    if refusal.retry_after is not None:
        response["Retry-After"] = str(refusal.retry_after)
    return response


def _fail(message: str, *, code: str, details: Any = None) -> HttpResponse:
    return _answer(Refusal(code, message, details))


def _answering(principal: Principal, view: Callable[[], HttpResponse]) -> HttpResponse:
    """What ``view`` answers, or the refusal for what it raised, recorded when it is a denial.

    The one place an API view's failure becomes its answer, so none reaches
    the client as an HTML error page.
    """

    try:
        return view()
    except Exception as exc:  # noqa: BLE001 - every failure is answered in the one refusal shape
        return _answer(refusal_of(exc, principal=principal))


def _bearer(request: HttpRequest) -> str:
    """The token the Authorization header carries, or "" without one."""

    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


def _operator(request: HttpRequest) -> AbstractBaseUser | None:
    """The signed-in operator, for a request with a session and no token."""

    if "Authorization" in request.headers:
        return None
    user: AbstractBaseUser | None = signed_in(request)
    return user


def _target(kind: str, words: str) -> dict[str, Any]:
    """The target's schema for a capability whose target is of ``kind``, said in its own words."""

    schema: dict[str, Any] = (
        {"oneOf": [{"type": "integer"}, {"type": "string", "pattern": INTEGER_TARGET}], "examples": [42]}
        if kind == "integer"
        else {"type": "string", "minLength": 1, "examples": [f"example-{kind.replace('_', '-')}"]}
    )
    return {
        **schema,
        "description": f"{words.rstrip('.')}: its {'id' if kind == 'integer' else kind.replace('_', ' ')}.",
    }


def request_schema(spec: Mapping[str, Any]) -> dict[str, Any]:
    """The body one capability takes: ``CallEnvelope``, its command under ``payload`` and its target typed by kind."""

    # Pydantic emits local refs such as ``#/$defs/Record``. Once the command
    # schema is nested under ``payload`` those refs still resolve from the
    # document root, so hoist its definitions into the envelope root instead
    # of publishing a schema that only looks valid for flat commands.
    command = {key: value for key, value in spec["input_schema"].items() if key != "$defs"}
    properties = dict(ENVELOPE_SCHEMA["properties"])
    envelope_payload = properties["payload"]
    # The command's own words say more than the envelope's, where it has them.
    properties["payload"] = {
        **command,
        "description": command.get("description", envelope_payload["description"]),
    }
    required: list[str] = ["payload"] if command.get("required") else []
    if kind := spec.get("target"):
        properties["target"] = _target(kind, spec.get("target_help") or properties["target"]["description"])
        required.append("target")
    else:
        del properties["target"]
    schema: dict[str, Any] = {**ENVELOPE_SCHEMA, "properties": properties, "required": required}
    if definitions := {**ENVELOPE_SCHEMA.get("$defs", {}), **spec["input_schema"].get("$defs", {})}:
        schema["$defs"] = definitions
    return schema


def _unserved(request: HttpRequest, methods: tuple[str, ...], *, configured: bool) -> HttpResponse | None:
    """The refusal an endpoint answers before it asks who is calling, if any."""

    if not configured:
        return _fail(
            "The machine API is not configured on this deployment.",
            code="not_configured",
        )
    if request.method not in methods:
        response = _fail(
            f"{request.method} is not allowed here.",
            code="method_not_allowed",
        )
        response["Allow"] = ", ".join(methods)
        return response
    return None


def _authenticated(request: HttpRequest, principal: Principal) -> APIRequest:
    # The one place this is set; every endpoint reads it.
    authenticated = cast(APIRequest, request)
    authenticated.principal = principal
    return authenticated


def _run_for_operator(
    request: HttpRequest, user: AbstractBaseUser, route: View, view: Callable[[APIRequest], HttpResponse]
) -> HttpResponse:
    """``view`` for a signed-in operator's request, while the rule every page opens by opens ``route`` to them."""

    refused = page_refusal(user, route)
    if refused is not None:
        return _answer(refused)
    operator = web_principal(user)
    return _answering(operator, lambda: view(_authenticated(request, operator)))


def _admit_token(request: HttpRequest) -> APIRequest | HttpResponse:
    """A bearer token's request, admitted as /mcp/ admits one, and refused at
    the door when a brake applies: an HTTP client has no tool result to carry it."""

    source = client_ip(request)
    admitted = agent_admission.admit(_bearer(request), interface=API_INTERFACE, source=source)
    if isinstance(admitted, agent_admission.Unadmitted):
        return _fail(admitted.reason, code=admitted.code)
    refusal = agent_admission.refused(admitted, source=source)
    if refusal is not None:
        return _refused(refusal)
    with agent_admission.carrying(admitted) as principal:
        return _authenticated(request, principal)


def _refused(refusal: AgentRefusal | Limited) -> HttpResponse:
    """A brake, or asking too much, with when to ask again."""

    return _answer(refusal.refusal)


def _run_for_agent(request: HttpRequest, view: Callable[[APIRequest], HttpResponse]) -> HttpResponse:
    """``view`` for a bearer's request, while its agent holds one of its calls at once."""

    admitted = _admit_token(request)
    if isinstance(admitted, HttpResponse):
        return admitted
    with agent_admission.at_once(admitted.principal, source=client_ip(request)) as busy:
        return _answering(admitted.principal, lambda: view(admitted)) if busy is None else _refused(busy)


# What every endpoint can answer, admitting its caller or once its view fails.
ENDPOINT_CODES = answered_by(Door.ADMISSION, Door.ENDPOINT)


def _endpoint(
    methods: tuple[str, ...],
    *,
    title: str,
    data: Any,
    codes: Iterable[str] = (),
    operator_session: bool = False,
    page: str = "",
) -> Callable[[APIView], View]:
    """Authenticate, then put every failure in the same envelope.

    Every argument is the endpoint's declaration, which ``openapi.py`` reads:
    ``data`` is what a success carries under ``data`` (None: not enveloped),
    ``codes`` the error codes it answers beside ENDPOINT_CODES, ``title``
    the operation in a few words, and ``page`` the route of the web page that
    shows what it answers, for one that serves a single domain.
    """

    # ``operator_session`` lets a signed-in operator's session read a GET view
    # too, opened to them as a page is, and works while the token surface is
    # unconfigured.
    if operator_session and set(methods) - {"GET", "HEAD"}:
        raise ValueError("A session is accepted only on a safe method.")

    answered = ENDPOINT_CODES | frozenset(codes)

    def decorate(view: APIView) -> View:
        # CSRF-exempt by construction rather than by concession: these views
        # read the Authorization header and never the session cookie, so a
        # browser cannot make an authenticated request to them at all; an
        # operator's session is accepted only on a safe method, above.
        @csrf_exempt
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            operator = _operator(request) if operator_session else None
            # An operator's session does not depend on the token surface, so
            # whether that is configured is asked only without one.
            refused = _unserved(request, methods, configured=operator is not None or is_configured())
            if refused is not None:
                return refused
            if operator is not None:
                return _run_for_operator(request, operator, wrapper, lambda admitted: view(admitted, *args, **kwargs))
            return _run_for_agent(request, lambda admitted: view(admitted, *args, **kwargs))

        wrapper.__name__ = view.__name__
        wrapper.__doc__ = view.__doc__
        # A fact a test can read, rather than one a reviewer has to notice.
        # `/api/` is exempt from the login *redirect*, so a view added here
        # without this decorator is not merely unprotected by convention: it
        # is served to anyone who asks. `core.tests.test_security` walks these routes
        # and fails if one lacks the mark.
        wrapper.__dict__.update(
            __hq_authenticated__=True,
            __hq_methods__=methods,
            __hq_title__=title,
            __hq_data__=data,
            __hq_codes__=answered,
            __hq_operator_session__=operator_session,
            __hq_page__=page,
        )
        return wrapper

    return decorate


@_endpoint(("GET",), title="API root", data=RootData)
def root(request: APIRequest) -> HttpResponse:
    """What this is, and what the presented credential may actually do."""

    # The reads root_links() types, so the document and the answer name the same ones.
    links = {name: reverse(f"hq_api:{name}") for name in _linked()}
    data: RootData = {
        "service": "severino-hq",
        "api_version": CURRENT_API_VERSION,
        "resource": settings.SEVERINO_API_RESOURCE,
        "actor": request.principal.actor,
        "granted": sorted(request.principal.granted),
        "links": links,
    }
    return _ok(data)


@_endpoint(("GET",), title="OpenAPI document", data=None, operator_session=True)
def openapi(_request: APIRequest) -> HttpResponse:
    """This API as an OpenAPI 3.2 document, derived from its routes and registries.

    Never anonymous: it names every capability, grant and resource kind, which
    /capabilities/ already keeps from anonymous callers. The signed-in operator
    reads it too (the reference page at /api/docs/), including where no API
    resource is configured: it describes the code, not a credential.
    """

    return _json(document())


@_endpoint(("GET",), title="List capabilities", data=CapabilityRequestCatalog, codes=("invalid_input",))
def capabilities(request: APIRequest) -> HttpResponse:
    """Every capability a token could ever hold, flagged by whether this one may run it.

    Not just the permitted slice: being told a capability exists but is not
    granted is what gets someone to fix a scope, where an empty list looks like
    a broken server.
    """

    # `?name=` narrows to a capability or a dotted prefix of whole segments,
    # and `?resource=` to the capabilities acting on that resource.
    described = describe_capabilities(principal=request.principal, **_narrowing(request, CAPABILITY_QUERY))
    specs: list[CapabilityDescription] = described["capabilities"]
    catalog: CapabilityRequestCatalog = {
        "schema_version": described["schema_version"],
        "capabilities": [
            {
                **spec,
                "idempotency_key_required": key_required(spec["effect"], request.principal),
                "request_schema": request_schema(spec),
            }
            for spec in specs
        ],
    }
    return _ok(catalog)


CAPABILITY_QUERY: Mapping[str, Mapping[str, Any]] = {
    "name": {
        "type": "string",
        "description": (
            "A capability's name, or a prefix of whole dotted segments: `infrastructure` keeps every "
            "`infrastructure.*`. Absent, every capability."
        ),
    },
    "resource": {
        "type": "string",
        "description": "Only the capabilities that act on this resource, as /resources/ names it.",
    },
}
# The narrowing inputs, for the OpenAPI document's query parameters.
capabilities.__dict__.update(__hq_query_fields__=CAPABILITY_QUERY)


@_endpoint(("GET",), title="List resources", data=ResourceCatalog, codes=("invalid_input", "unknown_resource"))
def resources(request: APIRequest) -> HttpResponse:
    """Every readable resource, including operations this token may use.

    Each list is named by the fields it narrows by; `?name=` answers that one
    resource with its list's whole query schema.
    """

    return _ok(describe_resources(principal=request.principal, **_narrowing(request, RESOURCE_QUERY)))


RESOURCE_QUERY: Mapping[str, Mapping[str, Any]] = {
    "name": {
        "type": "string",
        "description": "One resource, answered with its list's whole query schema. Absent, every resource.",
    }
}
# The narrowing input, for the OpenAPI document's query parameters.
resources.__dict__.update(__hq_query_fields__=RESOURCE_QUERY)


QueryFields = Mapping[str, Mapping[str, Any]]
_NULL: Mapping[str, Any] = {"type": "null"}


def query_fields(model: type[BaseModel]) -> dict[str, dict[str, Any]]:
    """Each input ``model`` takes, as the schema of its query parameter: one
    not sent is absent, so a ``null`` the model allows is no value it can be."""

    fields: dict[str, dict[str, Any]] = {}
    for name, schema in json_schema(model)["properties"].items():
        said = {key: value for key, value in schema.items() if key != "anyOf" and (key, value) != ("default", None)}
        branches = [branch for branch in schema.get("anyOf", ()) if branch != _NULL]
        if len(branches) == 1:
            said = {**branches[0], **said}
        elif branches:
            said["anyOf"] = branches
        fields[name] = said
    return fields


class _UnreadableQuery(QueryRefusal):
    """A declared parameter sent more than once."""


# A count in ASCII digits, signed as a JSON integer may be, short enough that
# int() never meets its digit limit; a plus, a space, an underscore or another
# script's digits stays text.
_COUNT = re.compile(r"-?[0-9]{1,18}")


def _narrowing(request: APIRequest, query_fields: QueryFields) -> dict[str, Any]:
    """The declared query parameters, each sent at most once, an integer one in digits read as that integer.

    Text sent empty is absent, as a form leaves it, so the read's own default
    applies. Anything but a count stays the text it was, which the read's
    strict model refuses in the words MCP's argument model uses for the same
    input.
    """

    values: dict[str, Any] = {}
    for field, schema in query_fields.items():
        sent = request.GET.getlist(field)
        if len(sent) > 1:
            raise _UnreadableQuery(f"Query field {field!r} may appear only once.")
        if not sent:
            continue
        (raw,) = sent
        if schema.get("type") == "integer":
            values[field] = int(raw) if _COUNT.fullmatch(raw) else raw
        elif text := raw.strip():
            values[field] = text
    return values


def _projection(
    serve: Callable[..., Any],
    answers: Any,
    query: type[BaseModel],
    name: str,
    subject: str,
    title: str,
    doc: str,
    page: str,
) -> View:
    """One principal-scoped projection, served with its query model's inputs.

    `topology` and `findings` are the same adapter, so it is written once.
    ``answers`` is the type ``serve`` returns, and ``query`` the model ``serve``
    judges its narrowing by, which MCP's tool takes as its arguments.
    ``subject`` opens the refusal of an input the model refuses: the read's MCP
    tool name, so both doors say it alike.
    """

    fields = query_fields(query)

    def view(request: APIRequest) -> HttpResponse:
        # A repeat, or a focus that names nothing, is answered as
        # invalid_input in HQ's own words, never the value sent.
        narrowing = _narrowing(request, fields)
        try:
            return _ok(serve(principal=request.principal, **narrowing))
        except PydanticValidationError as exc:
            return _answer(refusal_of(exc, name=subject))

    # Named explicitly: `_endpoint` copies these onto its wrapper, and the
    # route-walking security test reads them.
    view.__name__ = name
    view.__doc__ = doc
    served = _endpoint(("GET",), title=title, data=answers, codes=("invalid_input",), page=page)(view)
    # The narrowing inputs, for the OpenAPI document's query parameters.
    served.__dict__.update(__hq_query_fields__=fields)
    return served


topology: View = _projection(
    application_topology,
    TopologyAnswer,
    TopologyQuery,
    "topology",
    "get_topology",
    "Read the topology",
    """The live permitted infrastructure graph and its canonical actions.

    `?lens=` narrows to a standing question. `?focus=`, `direction`, and
    bounded `depth` trace a dependency neighborhood inside that projection.
    With neither a lens nor a focus it answers the counts and the lenses.
    """,
    "control_plane:topology",
)

findings: View = _projection(
    application_findings,
    Findings,
    FindingsQuery,
    "findings",
    "get_findings",
    "List findings",
    """What HQ currently claims is wrong, with the evidence and a remedy.

    Derived from the same projection as the topology and narrowed the same way,
    so a finding can never name something the token could not already read. A
    remedy is a reference to an existing capability, never a new route.
    `?severity=` is a floor and `?limit=` caps how many are returned, most
    severe first; the summary still counts every finding.
    """,
    "control_plane:findings",
)


@_endpoint(("GET",), title="List connections", data=ConnectionCatalog, page="control_plane:connections")
def connections(request: APIRequest) -> HttpResponse:
    """Connection contracts and whether this token is permitted each.

    A family's instances are the ``connections`` resource.
    """

    return _ok(describe_connections(principal=request.principal))


RESOURCE_CODES = answered_by(Door.RESOURCE)


def _selection(raw: str | None) -> list[str] | None:
    """``?fields=a,b`` as the field mask it is (``FIELD_MASK``), or ``None`` without one."""

    if raw is None:
        return None
    return [part for part in (piece.strip() for piece in raw.split(",")) if part]


@_endpoint(("GET",), title="List a resource", data=ResourceCollection, codes=RESOURCE_CODES)
def resource_list(request: APIRequest, name: str) -> HttpResponse:
    """List one resource through its declared, schema-validated query."""

    filters: dict[str, str] = {}
    for key, values in request.GET.lists():
        if len(values) != 1:
            return _fail(
                f"Query field {key!r} may appear only once.",
                code="invalid_input",
            )
        filters[key] = values[0]
    fields = _selection(filters.pop(RESERVED_QUERY_FIELD, None))
    return _ok(list_application_resource(name, filters, principal=request.principal, strict=False, fields=fields))


@_endpoint(("GET",), title="Get a resource record", data=dict[str, Any], codes=RESOURCE_CODES)
def resource_detail(request: APIRequest, name: str, identifier: str) -> HttpResponse:
    """Get one resource record through its declared identifier contract."""

    asked = request.GET.getlist(RESERVED_QUERY_FIELD)
    if len(asked) > 1:
        return _fail(f"Query field {RESERVED_QUERY_FIELD!r} may appear only once.", code="invalid_input")
    fields = _selection(asked[0] if asked else None)
    return _ok(get_application_resource(name, identifier, principal=request.principal, strict=False, fields=fields))


class EnvelopeError(Exception):
    """A request body that never reaches a capability, and why."""

    def __init__(self, message: str, *, code: str = "invalid_input") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _body_json(request: HttpRequest) -> dict[str, Any]:
    """The request body as a JSON object, or an EnvelopeError saying why not."""

    if request.content_type != "application/json":
        raise EnvelopeError(
            "Content-Type must be application/json.",
            code="unsupported_media_type",
        )
    try:
        body = request.body
    except RequestDataTooBig:
        raise EnvelopeError(
            "Request body exceeds this deployment's safety limit.",
            code="request_too_large",
        ) from None
    if not body:
        return {}
    try:
        payload = strict_json.loads(body)
    except ValueError, UnicodeDecodeError:
        raise EnvelopeError("Request body is not valid JSON.", code="invalid_json") from None
    if not isinstance(payload, dict):
        raise EnvelopeError("Request body must be a JSON object.", code="invalid_json")
    return payload


def execute_codes(spec: CapabilitySpec | None) -> frozenset[str]:
    """Every code a call of ``spec`` can be answered with; for None, a call by any name."""

    return call_codes(capability_doors(spec))


def call_codes(doors: Iterable[Door]) -> frozenset[str]:
    """Every code a capability call passing ``doors`` can be answered with: every
    endpoint's, the envelope's own and the call's."""

    return ENDPOINT_CODES | answered_by(Door.BODY, *doors)


# What a call answers under ``data``: the capability's own result, or that it waits for a person.
type CapabilityAnswer = Annotated[
    dict[str, Any] | AwaitingApproval,
    Field(description="What the capability answers, in the shape its command names, or that it waits for a person."),
]


@_endpoint(("POST",), title="Run a capability", data=CapabilityAnswer, codes=execute_codes(None))
def execute(request: APIRequest, name: str) -> HttpResponse:
    """Run one HQ capability, replaying machine writes by idempotency key."""

    try:
        body = _body_json(request)
    except EnvelopeError as exc:
        return _fail(exc.message, code=exc.code)
    envelope = read_body(name, body)
    if isinstance(envelope, Refusal):
        return _answer(envelope)

    result, replayed = run_capability(
        name,
        envelope.get("payload", {}),
        principal=request.principal,
        target=envelope.get("target"),
        expected_updated_at=envelope.get("expected_updated_at"),
        idempotency_key=request.headers.get(IDEMPOTENCY_HEADER),
    )
    if result.get("ok", False):
        response = _ok({key: value for key, value in result.items() if key != "ok"})
    else:
        response = _answer(refusal_of(result))
    if replayed:
        response["Idempotency-Replayed"] = "true"
    return response
