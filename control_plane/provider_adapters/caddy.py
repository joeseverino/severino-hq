"""Caddy emits its declaration and every controller surface together."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import Field

from ..names import certificate_covers, normalized_hostname
from ..provider_spec import ProviderModel, ProviderSpec, applies
from .contracts import (
    ControllerIntegrationAdapter,
    ProviderError,
    ProviderResult,
    ProviderRuntime,
    ServedCertificate,
)

# Why a route carries no certificate, when the edge could not say.
_NO_CERTIFICATE_OPERATION = (
    "the edge target does not report its certificates; redeploy it at version 4 or later"
)
_SOME_UNREPORTED = (
    "the edge loads {loaded} certificates from files and its target reports {read}; "
    "redeploy it at version 4 or later"
)
_MANAGED_BY_CADDY = (
    "Caddy manages this name's certificate itself, and the edge reports only the "
    "certificate it loads from a file"
)

CADDY_ROUTE_KIND = "caddy.route"


# A Caddy placeholder: text Caddy replaces while it handles each request.
_PLACEHOLDER = re.compile(r"\{[^{}\s]+\}")
# The placeholders that stand for the host the request itself names.
_REQUESTED_HOST = re.compile(r"^\{http\.request\.host(?:port)?\}(?::(?P<port>[0-9]{1,5}))?$")


def decided_per_request(upstream: Any) -> bool:
    """Whether an upstream is a placeholder Caddy fills in for each request.

    Such an upstream names no machine, container or port of its own, so it is
    never an address to resolve or locate.
    """

    return bool(_PLACEHOLDER.search(str(upstream or "")))


def to_requested_host(upstream: Any) -> bool:
    """Whether a route forwards to the host each request names, not to a fixed target."""

    return bool(_REQUESTED_HOST.fullmatch(str(upstream or "").strip()))


def _hands_off_to(upstream: str) -> str:
    """Where a route sends requests, as a sentence fragment for its readout."""

    if not upstream:
        return "Caddy answers this itself"
    matched = _REQUESTED_HOST.fullmatch(upstream)
    if matched:
        port = matched.group("port")
        return "the host each request names" + (f", on port {port}" if port else "")
    if decided_per_request(upstream):
        return f"decided per request ({upstream})"
    return upstream


def upstreams(node: Any) -> list[str]:
    """Every address a possibly nested Caddy handler tree forwards to."""

    found: list[str] = []
    if isinstance(node, dict):
        if node.get("handler") == "reverse_proxy":
            for upstream in node.get("upstreams") or ():
                dial = str((upstream or {}).get("dial", "") or "").strip()
                if dial:
                    found.append(dial)
        for value in node.values():
            found.extend(upstreams(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(upstreams(item))
    return found


def routes(config: dict[str, Any], connection_ref: str) -> list[dict[str, Any]]:
    """Return one record per hostname this Caddy answers for."""

    servers = (((config or {}).get("apps") or {}).get("http") or {}).get(
        "servers"
    ) or {}
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for server in servers.values():
        for route in (server or {}).get("routes") or ():
            hosts = [
                normalized_hostname(host)
                for match in (route or {}).get("match") or ()
                for host in (match or {}).get("host") or ()
                if str(host).strip()
            ]
            if not hosts:
                continue
            # One address named by several handlers is one destination.
            destinations = list(dict.fromkeys(upstreams(route.get("handle"))))
            upstream = destinations[0] if len(destinations) == 1 else ""
            for host in hosts:
                found.setdefault(
                    (connection_ref, host),
                    {
                        "connection_ref": connection_ref,
                        "domain": host,
                        # A placeholder is kept as Caddy holds it.
                        "upstream": upstream,
                        "to_requested_host": to_requested_host(upstream),
                    },
                )
    return list(found.values())


def loaded_files(config: dict[str, Any]) -> int:
    """How many certificate files the config loads, rather than obtaining its own."""

    tls = ((config or {}).get("apps") or {}).get("tls") or {}
    entries = (tls.get("certificates") or {}).get("load_files") or ()
    return len(
        {
            str(entry.get("certificate", "") or "") if isinstance(entry, dict) else str(entry)
            for entry in entries
        }
    )


def certificate_facts(pem: bytes) -> dict[str, Any]:
    """The first certificate in ``pem`` as a route states it."""

    from cryptography import x509

    return _facts(x509.load_pem_x509_certificate(pem))


def loaded_certificates(pem: bytes) -> list[dict[str, Any]]:
    """Every certificate in ``pem``, each as a route states it, in the order given."""

    from cryptography import x509

    return [_facts(leaf) for leaf in x509.load_pem_x509_certificates(pem)]


def _facts(leaf: Any) -> dict[str, Any]:
    """One leaf's names, issuer and expiry. Public facts only."""

    from cryptography import x509
    from cryptography.x509.oid import ExtensionOID, NameOID

    def first(name, oid) -> str:
        found = name.get_attributes_for_oid(oid)
        return str(found[0].value) if found else ""

    try:
        names = leaf.extensions.get_extension_for_oid(
            ExtensionOID.SUBJECT_ALTERNATIVE_NAME
        ).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        names = []
    domains = tuple(dict.fromkeys(normalized_hostname(name) for name in names if name))
    return {
        "name": first(leaf.subject, NameOID.COMMON_NAME) or (domains[0] if domains else ""),
        "provider": first(leaf.issuer, NameOID.ORGANIZATION_NAME)
        or first(leaf.issuer, NameOID.COMMON_NAME),
        "expires_on": leaf.not_valid_after_utc.isoformat(),
        "domains": domains,
    }


def _leaves(runtime: ProviderRuntime, connection_ref: str) -> bytes:
    """The leaf of every certificate file the edge loads, as PEM.

    A target older than version 4 has no ``certificates`` operation and answers
    ``certificate`` with the one leaf in its certificate directory.
    """

    try:
        return runtime.ssh(connection_ref, "certificates")
    except ProviderError:
        return runtime.ssh(connection_ref, "certificate")


def _served(
    runtime: ProviderRuntime, connection_ref: str, config: dict[str, Any]
) -> tuple[list[dict[str, Any]], str]:
    """``(certificates, unread)``: every file certificate this edge serves, and
    why a route none of them covers names no certificate."""

    loaded = loaded_files(config)
    if not loaded:
        return [], _MANAGED_BY_CADDY
    try:
        found = loaded_certificates(_leaves(runtime, connection_ref))
    except ProviderError:
        return [], _NO_CERTIFICATE_OPERATION
    except (OSError, ValueError) as exc:
        return [], f"its certificate did not parse ({type(exc).__name__})"
    if not found:
        return [], _NO_CERTIFICATE_OPERATION
    if len(found) < loaded:
        return found, _SOME_UNREPORTED.format(loaded=loaded, read=len(found))
    return found, ""


def covering_certificate(
    domain: str, certificates: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """The loaded certificate a name is served with, or None when none covers it.

    A certificate naming the host itself is chosen over one covering it by
    wildcard, and of two that match alike the one valid longest, which is how
    Caddy chooses among the certificates it holds.
    """

    name = normalized_hostname(domain)
    covering = [
        certificate
        for certificate in certificates
        if certificate_covers(name, frozenset(certificate.get("domains") or ()))
    ]
    if not covering:
        return None
    return max(
        covering,
        key=lambda certificate: (
            name in (certificate.get("domains") or ()),
            str(certificate.get("expires_on", "")),
        ),
    )


def with_certificates(
    found: list[dict[str, Any]], certificates: list[dict[str, Any]], unread: str
) -> list[dict[str, Any]]:
    """Each route with the loaded certificate that covers its name, when one does."""

    for route in found:
        certificate = covering_certificate(route["domain"], certificates)
        if certificate is not None:
            route["certificate"] = certificate
        else:
            route["certificate_unread"] = unread or _MANAGED_BY_CADDY
    return found


def served_certificate(record: dict[str, Any]) -> ServedCertificate | None:
    """The certificate a route record serves its name with, or why it cannot say."""

    domain = normalized_hostname(record.get("domain"))
    certificate = record.get("certificate") or {}
    if isinstance(certificate, dict) and certificate.get("name"):
        return ServedCertificate((domain,), certificate)
    unread = str(record.get("certificate_unread", "") or "")
    return ServedCertificate((domain,), {}, unread=unread) if unread else None


def inventory(runtime: ProviderRuntime) -> list[dict[str, Any]]:
    """Read the SSH connections that serve Caddy.

    Declared where anything declares it. Asking every SSH connection and
    keeping whichever answered means identifying a Caddy host by trying to use
    it as one, and a connection that is not a Caddy host is asked again on
    every sweep: forever, since nothing about the answer changes. Against a
    machine somebody else operates that is a failed login every couple of
    minutes, which is a cost paid at their end.

    Falls back to asking everything only while nothing carries a role at all,
    so a fleet that has never declared one still discovers its edges.
    """

    declared = runtime.connection_refs_for_role("caddy")
    found: list[dict[str, Any]] = []
    for connection_ref in declared or runtime.ssh_connection_refs():
        try:
            payload = runtime.ssh(connection_ref, "routes")
        except (ProviderError, OSError, ValueError):
            continue
        try:
            config = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(config, dict):
            certificates, unread = _served(runtime, connection_ref, config)
            found.extend(with_certificates(routes(config, connection_ref), certificates, unread))
    return found


# What may reach the Caddyfile from a declaration. The file is text, so a value
# carrying a newline or a brace would become directives of its own (a second
# site, a file server, an import), and the typed route would be arbitrary edge
# configuration. Each is one token: a hostname (a wildcard allowed), an upstream
# as host:port or scheme://host:port, a plain absolute directory.
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
DOMAIN = rf"^(?:\*\.)?{_LABEL}(?:\.{_LABEL})*\.?$"
UPSTREAM = r"^(?:(?:https?|h2c)://)?[A-Za-z0-9](?:[A-Za-z0-9._-]*|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?$"
DIRECTORY = r"^(?:/[A-Za-z0-9._-]+)+/?$"


def _token(value: str, pattern: str, what: str) -> str:
    if not re.fullmatch(pattern, value):
        raise ProviderError(f"A Caddy route's {what} is not one plain value: {value!r}.")
    return value


def _route_block(spec: dict[str, Any], certificate_directory: str) -> str:
    # Checked again here, not only by the models: this is the line that writes
    # the file, and nothing may reach it that the models would refuse.
    domain = _token(str(spec["domain"]), DOMAIN, "hostname")
    upstream = _token(str(spec["upstream"]), UPSTREAM, "upstream")
    if certificate_directory:
        _token(certificate_directory, DIRECTORY, "certificate directory")
    lines = [f"{domain} {{"]
    if certificate_directory:
        directory = certificate_directory.rstrip("/")
        lines.append(f"\ttls {directory}/fullchain.pem {directory}/privkey.pem")
    lines.append(f"\treverse_proxy {upstream}")
    lines.append("}")
    return "\n".join(lines)


def render_routes(specs: list[dict[str, Any]], certificate_directory: str = "") -> str:
    """Render every declared route for one edge as the complete HQ-owned file."""

    ordered = sorted(
        (spec for spec in specs if spec.get("domain") and spec.get("upstream")),
        key=lambda spec: spec["domain"],
    )
    header = (
        "# Written by Severino HQ. Edits here are replaced on the next reconcile;\n"
        "# routes this file does not name are the operator's and are untouched.\n"
    )
    return (
        header
        + "\n"
        + "\n\n".join(_route_block(spec, certificate_directory) for spec in ordered)
        + "\n"
    )


def reconcile(
    runtime: ProviderRuntime,
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    """Converge the complete route file represented by one resolved resource."""

    del observed
    rendered = render_routes(
        [dict(route) for route in spec.get("routes", ())],
        spec.get("certificate_directory", ""),
    )
    if not apply:
        return ProviderResult(
            changed=False,
            status={"routes": len(spec.get("routes", ()))},
            conditions=[],
            message="Would write the routes this edge serves.",
        )
    runtime.ssh(spec["connection_ref"], "routes:write", rendered.encode("utf-8"))
    return ProviderResult(
        changed=True,
        status={"routes": len(spec.get("routes", ()))},
        conditions=[
            runtime.condition(
                "Ready", True, "Written", "Caddy reloaded with these routes."
            )
        ],
        message="Routes written and Caddy reloaded.",
    )


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
ADAPTER = ControllerIntegrationAdapter(
    definitions=(DEFINITION,),
    inventory={DEFINITION.kind: inventory},
    connection_probes={},
    actions={(DEFINITION.kind, "reconcile"): reconcile},
)
