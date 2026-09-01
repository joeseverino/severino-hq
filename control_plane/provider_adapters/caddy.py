"""Caddy emits its declaration and every controller surface together."""

from __future__ import annotations

import json
from typing import Any

from pydantic import Field

from .contracts import (
    ControllerProviderAdapter,
    ProviderError,
    ProviderResult,
    ProviderRuntime,
)


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
                str(host).strip().lower().rstrip(".")
                for match in (route or {}).get("match") or ()
                for host in (match or {}).get("host") or ()
                if str(host).strip()
            ]
            if not hosts:
                continue
            destinations = upstreams(route.get("handle"))
            for host in hosts:
                found.setdefault(
                    (connection_ref, host),
                    {
                        "connection_ref": connection_ref,
                        "domain": host,
                        "upstream": (
                            destinations[0] if len(destinations) == 1 else ""
                        ),
                    },
                )
    return list(found.values())


def inventory(runtime: ProviderRuntime) -> list[dict[str, Any]]:
    """Read every SSH connection that identifies itself as a Caddy edge."""

    found: list[dict[str, Any]] = []
    for connection_ref in runtime.ssh_connection_refs():
        try:
            payload = runtime.ssh(connection_ref, "routes")
        except (ProviderError, OSError, ValueError):
            continue
        try:
            config = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(config, dict):
            found.extend(routes(config, connection_ref))
    return found


def _route_block(spec: dict[str, Any], certificate_directory: str) -> str:
    lines = [f"{spec['domain']} {{"]
    if certificate_directory:
        directory = certificate_directory.rstrip("/")
        lines.append(f"\ttls {directory}/fullchain.pem {directory}/privkey.pem")
    lines.append(f"\treverse_proxy {spec['upstream']}")
    lines.append("}")
    return "\n".join(lines)


def render_routes(
    specs: list[dict[str, Any]], certificate_directory: str = ""
) -> str:
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
        + "\n\n".join(
            _route_block(spec, certificate_directory) for spec in ordered
        )
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
            if route.get("connection_ref") == connection_ref
            and route.get("upstream")
        ],
    }


def build_adapter(*, provider_model, provider_spec, applies, normalized_hostname):
    class CaddyRouteSpec(provider_model):
        connection_ref: str = Field(
            default="",
            max_length=160,
            title="Caddy",
            description=(
                "The credential that reaches the host this route is served from."
            ),
        )
        domain: str = Field(
            min_length=1,
            max_length=253,
            title="Hostname",
            description="The name this route answers for.",
        )
        upstream: str = Field(
            default="",
            max_length=253,
            title="Hands off to",
            description=(
                "Where Caddy sends the request -- a container and port, usually."
            ),
        )

    class CaddyRouteInFile(provider_model):
        domain: str = Field(min_length=1, max_length=253)
        upstream: str = Field(min_length=1, max_length=253)

    class ResolvedCaddyRouteSpec(CaddyRouteSpec):
        certificate_directory: str = Field(default="", max_length=500)
        routes: list[CaddyRouteInFile] = Field(default_factory=list)

    def identity(spec: dict[str, Any]) -> tuple[str, ...]:
        return (
            str(spec.get("connection_ref", "") or ""),
            normalized_hostname(str(spec.get("domain", "") or "")),
        )

    return ControllerProviderAdapter(
        definition=provider_spec(
            "caddy.route",
            "A name an edge Caddy serves, and where it hands the request on.",
            CaddyRouteSpec,
            ResolvedCaddyRouteSpec,
            _resolve,
            actions={"reconcile": applies(automatic=True)},
            label="Caddy route",
            connection_providers=("ssh",),
            facet="proxy",
            hostnames=lambda spec: (spec["domain"],),
            origin=lambda spec: str(spec.get("upstream", "") or "").strip(),
            identity=identity,
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
                    str(spec.get("upstream", "") or "")
                    or "Caddy answers this itself",
                ),
            ),
            sample_record={
                "connection_ref": "an-edge",
                "domain": "app.example.com",
                "upstream": "app:8080",
            },
            removal_gap=(
                "Removing the declaration must take the route with it -- forgetting "
                "it would leave the edge serving a route nothing points at. The "
                "controller has no delete for this yet."
            ),
        ),
        inventory=inventory,
        connection_probes={},
        actions={"reconcile": reconcile},
    )
