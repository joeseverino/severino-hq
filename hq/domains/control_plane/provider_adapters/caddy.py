"""Caddy: the routes an edge serves, as declared. The controller reads and writes them."""

import re
from typing import Annotated, Any

from pydantic import Field

from ..names import normalized_hostname
from ..provider_spec import ProviderModel, ProviderSpec, SharedValue, applies
from .contracts import ServedCertificate

CADDY_ROUTE_KIND = "caddy.route"


# A Caddy placeholder: text Caddy replaces while it handles each request.
_PLACEHOLDER = re.compile(r"\{[^{}\s]+\}")
# The placeholders that stand for the host the request itself names; its group
# is the fixed port, when one follows.
REQUESTED_HOST = r"^\{http\.request\.host(?:port)?\}(?::([0-9]{1,5}))?$"
_REQUESTED_HOST = re.compile(REQUESTED_HOST)


def decided_per_request(upstream: Any) -> bool:
    """Whether an upstream is a placeholder Caddy fills in for each request.

    Such an upstream names no machine, container or port of its own, so it is
    never an address to resolve or locate.
    """

    return bool(_PLACEHOLDER.search(str(upstream or "")))


def _hands_off_to(upstream: str) -> str:
    """Where a route sends requests, as a sentence fragment for its readout."""

    if not upstream:
        return "Caddy answers this itself"
    matched = _REQUESTED_HOST.fullmatch(upstream)
    if matched:
        port = matched.group(1)
        return "the host each request names" + (f", on port {port}" if port else "")
    if decided_per_request(upstream):
        return f"decided per request ({upstream})"
    return upstream


def served_certificate(record: dict[str, Any]) -> ServedCertificate | None:
    """The certificate a route record serves its name with, or why it cannot say."""

    domain = normalized_hostname(record.get("domain"))
    certificate = record.get("certificate") or {}
    if isinstance(certificate, dict) and certificate.get("name"):
        return ServedCertificate((domain,), certificate)
    unread = str(record.get("certificate_unread", "") or "")
    return ServedCertificate((domain,), {}, unread=unread) if unread else None


# What may reach the Caddyfile from a declaration. The file is text, so a value
# carrying a newline or a brace would become directives of its own (a second
# site, a file server, an import), and the typed route would be arbitrary edge
# configuration. Each is one token: a hostname (a wildcard allowed), an upstream
# as host:port or scheme://host:port, a plain absolute directory. They are
# shared values (``SHARED``): the controller (controller/providers/caddy.go)
# checks the same patterns where it writes the file.
DOMAIN = r"^(?:\*\.)?[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?$"
UPSTREAM = r"^(?:(?:https?|h2c)://)?[A-Za-z0-9](?:[A-Za-z0-9._-]*|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?$"
DIRECTORY = r"^(?:/[A-Za-z0-9._-]+)+/?$"


def _resolve(authored: dict[str, Any], context: Any) -> dict[str, Any]:
    connection_ref = authored.get("connection_ref", "")
    directory = ""
    for target in context.delivery_targets:
        if (
            target.get("connection_ref") == connection_ref
            and target.get("kind") == "caddy"
        ):
            directory = str(target.get("certificate_directory", "") or "")
    return {
        **authored,
        "certificate_directory": directory,
        "routes": [
            {
                "domain": route.get("domain", ""),
                "upstream": route.get("upstream", ""),
            }
            for route in (context.caddy_routes() if context.caddy_routes else ())
            if route.get("connection_ref") == connection_ref and route.get("upstream")
        ],
    }


class CaddyRouteSpec(ProviderModel):
    connection_ref: str = Field(
        default="",
        max_length=160,
        title="Caddy",
        description=(
            "The connection to the host that serves this route."
        ),
    )
    domain: str = Field(
        min_length=1,
        max_length=253,
        pattern=DOMAIN,
        title="Hostname",
        description="The name this route answers for.",
    )
    upstream: str = Field(
        default="",
        max_length=253,
        pattern=rf"^(?:|{UPSTREAM[1:-1]})$",
        title="Hands off to",
        description=(
            "Where Caddy sends the request, usually a container and port."
        ),
    )

class CaddyRouteInFile(ProviderModel):
    domain: str = Field(min_length=1, max_length=253, pattern=DOMAIN)
    upstream: str = Field(min_length=1, max_length=253, pattern=UPSTREAM)

class ResolvedCaddyRouteSpec(CaddyRouteSpec):
    certificate_directory: str = Field(default="", max_length=500, pattern=rf"^(?:|{DIRECTORY[1:-1]})$")
    routes: list[CaddyRouteInFile] = Field(default_factory=list)

def _origin(spec: dict[str, Any]) -> str:
    """The fixed address a route forwards to, or "" when it has none.

    A route Caddy answers itself has none, and neither has one whose upstream
    is decided per request: a placeholder is not a hostname or a port.
    """

    upstream = str(spec.get("upstream", "") or "").strip()
    return "" if decided_per_request(upstream) else upstream


def _identity(spec: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(spec.get("connection_ref", "") or ""),
        normalized_hostname(str(spec.get("domain", "") or "")),
    )

DEFINITION = ProviderSpec(
    CADDY_ROUTE_KIND,
    "A hostname an edge Caddy serves, and where it sends requests.",
    CaddyRouteSpec,
    ResolvedCaddyRouteSpec,
    _resolve,
    actions={"reconcile": applies(automatic=True)},
    label="Caddy route",
    connection_providers=("ssh",),
    facet="proxy",
    hostnames=lambda spec: (spec["domain"],),
    origin=_origin,
    served_certificate=served_certificate,
    identity=_identity,
    from_record=lambda record: {
        "connection_ref": str(record.get("connection_ref", "") or ""),
        "domain": str(record.get("domain", "") or ""),
        "upstream": str(record.get("upstream", "") or ""),
    },
    key_hint=lambda spec: (
        f"{normalized_hostname(str(spec.get('domain', '') or ''))}-caddy"
    ),
    readout=lambda spec, status: (
        (
            "Served by",
            "",
            f"caddy on {spec.get('connection_ref', '') or 'the edge'}",
        ),
        (
            "Hands off to",
            "",
            _hands_off_to(str(spec.get("upstream", "") or "").strip()),
        ),
    ),
    sample_record={
        "connection_ref": "an-edge",
        "domain": "app.example.com",
        "upstream": "app:8080",
        "to_requested_host": False,
    },
    # HQ's file holds only the routes it declares, so a route no declaration
    # accounts for is in the operator's Caddyfile. Declaring it would write a
    # second site block for a name the operator's file already serves.
    adoption_gap=(
        "This route is in the edge's own Caddyfile, which HQ reads and never "
        "writes. Declare a route to have HQ serve a name from its own file."
    ),
    removal_gap=(
        "The controller cannot delete Caddy routes yet, so the edge would "
        "keep serving it."
    ),
)
DEFINITIONS = (DEFINITION,)

SHARED = (
    SharedValue(
        "CaddyRouteInFile",
        CaddyRouteInFile,
        "One route in the file HQ writes on a Caddy edge. The file is text, so each "
        "value is one token: a hostname (a wildcard allowed) and an upstream as "
        "host:port or scheme://host:port. HQ validates a declaration with these "
        "patterns and the controller checks them again on the line that writes the file.",
    ),
    SharedValue(
        "CaddyCertificateDirectory",
        Annotated[str, Field(max_length=500, pattern=DIRECTORY)],
        "The directory a Caddy edge loads delivered certificates from: one plain "
        "absolute path.",
    ),
    SharedValue(
        "CaddyRequestedHost",
        Annotated[str, Field(pattern=REQUESTED_HOST)],
        "An upstream that names the host each request names, with an optional fixed "
        "port (the captured group).",
    ),
)
