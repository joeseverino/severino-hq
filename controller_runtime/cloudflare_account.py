"""What a Cloudflare account holds beyond its DNS.

Pages projects, D1 databases, Access applications and service tokens, tunnels,
edge certificates and redirects, each read once per sweep.
"""

from __future__ import annotations

from typing import Any

from control_plane.provider_adapters.contracts import ProviderError
from control_plane.provider_adapters.parts import (
    refuse_part,
    unread_reason as _unread_reason,
)
from controller_runtime import redirects
from . import cloudflare_analytics, cloudflare_api, provider_http
from .handlers import reads


@reads("cloudflare.pages_project")
def list_pages_projects() -> list[dict[str, Any]]:
    """Pages projects and their latest production deployment."""

    projects = []
    for ref in cloudflare_api.cloudflare_api_refs():
        account = _cloudflare_account(ref)
        for project in _cloudflare_account_list(ref, "/pages/projects", per_page=10):
            deployment = project.get("canonical_deployment") or {}
            metadata = (deployment.get("deployment_trigger") or {}).get("metadata") or {}
            projects.append(
                {
                    "connection_ref": ref,
                    "account_id": account,
                    "name": project.get("name", ""),
                    "subdomain": project.get("subdomain") or "",
                    "domains": tuple(project.get("domains") or ()),
                    "production_branch": project.get("production_branch") or "",
                    "deployment_id": deployment.get("id") or "",
                    "deployment_commit": str(metadata.get("commit_hash") or "")[:7],
                    "deployment_created_on": deployment.get("created_on") or "",
                }
            )
    return projects


@reads("cloudflare.d1_database")
def list_d1_databases() -> list[dict[str, Any]]:
    """D1 databases. The list omits file_size, so each is read once more."""

    databases = []
    for ref in cloudflare_api.cloudflare_api_refs():
        account = _cloudflare_account(ref)
        for database in _cloudflare_account_list(ref, "/d1/database"):
            uuid = str(database.get("uuid", ""))
            record = {
                "connection_ref": ref,
                "account_id": account,
                "name": database.get("name", ""),
                "uuid": uuid,
                "created_at": database.get("created_at") or "",
                "version": database.get("version") or "",
            }
            try:
                detail = cloudflare_api.cloudflare_api_result(
                    f"/accounts/{account}/d1/database/{uuid}", ref
                )
            except (ProviderError, OSError, ValueError) as exc:
                refuse_part(
                    "file_size", exc, scope=str(record["name"]), connection_ref=ref
                )
            else:
                size = (detail or {}).get("file_size")
                if isinstance(size, int):
                    record["file_size"] = size
            databases.append(record)
    return databases


def _access_destination_hosts(destinations: Any) -> tuple[str, ...]:
    """Hostnames from an application's destinations, without paths or CIDRs."""

    hosts: list[str] = []
    for destination in destinations or ():
        if not isinstance(destination, dict):
            continue
        if destination.get("type") == "public":
            uri = str(destination.get("uri") or "")
            host = uri.split("://", 1)[-1].split("/", 1)[0]
        else:
            host = str(destination.get("hostname") or "")
        host = host.strip().lower().rstrip(".")
        if host and host not in hosts:
            hosts.append(host)
    return tuple(hosts)


@reads("cloudflare.access_app")
def list_access_apps() -> list[dict[str, Any]]:
    """Access applications at the account, with their policies by name."""

    apps = []
    for ref in cloudflare_api.cloudflare_api_refs():
        account = _cloudflare_account(ref)
        for app in _cloudflare_account_list(ref, "/access/apps"):
            apps.append(
                {
                    "connection_ref": ref,
                    "account_id": account,
                    "id": app.get("id", ""),
                    "name": app.get("name") or "",
                    "type": app.get("type") or "",
                    "domain": app.get("domain") or "",
                    "destinations": _access_destination_hosts(app.get("destinations")),
                    "session_duration": app.get("session_duration") or "",
                    "policies": tuple(
                        {"id": policy.get("id") or "", "name": policy.get("name") or ""}
                        for policy in app.get("policies") or ()
                        if isinstance(policy, dict)
                    ),
                }
            )
    return apps


def _admits_token(rule: Any, token_id: str) -> bool:
    if not isinstance(rule, dict):
        return False
    if "any_valid_service_token" in rule:
        return True
    return str((rule.get("service_token") or {}).get("token_id") or "") == token_id


def _apps_admitting(apps: list[dict[str, Any]], token_id: str) -> tuple[dict, ...]:
    return tuple(
        {"id": app.get("id") or "", "name": app.get("name") or ""}
        for app in apps
        if any(
            _admits_token(rule, token_id)
            for policy in app.get("policies") or ()
            if isinstance(policy, dict)
            for rule in policy.get("include") or ()
        )
    )


@reads("cloudflare.access_service_token")
def list_access_service_tokens() -> list[dict[str, Any]]:
    """Service tokens, and the applications whose policies include them.

    The client ID is never read into a record.
    """

    tokens = []
    for ref in cloudflare_api.cloudflare_api_refs():
        listed = _cloudflare_account_list(ref, "/access/service_tokens")
        try:
            apps = _cloudflare_account_list(ref, "/access/apps")
            unread = False
        except (ProviderError, OSError, ValueError) as exc:
            refuse_part("apps", exc, connection_ref=ref)
            apps, unread = [], True
        for token in listed:
            token_id = str(token.get("id", ""))
            record = {
                "connection_ref": ref,
                "id": token_id,
                "name": token.get("name") or "",
                "expires_at": token.get("expires_at") or "",
                "created_at": token.get("created_at") or "",
            }
            if not unread:
                record["apps"] = _apps_admitting(apps, token_id)
            tokens.append(record)
    return tokens


def _tunnel_ingress(config: Any) -> tuple[dict[str, str], ...]:
    rules = ((config or {}).get("config") or {}).get("ingress") or ()
    return tuple(
        {"hostname": str(rule["hostname"]), "service": str(rule.get("service") or "")}
        for rule in rules
        if isinstance(rule, dict) and rule.get("hostname")
    )


def _tunnel_connections(clients: Any) -> tuple[dict[str, str], ...]:
    found = []
    for client in clients or ():
        if not isinstance(client, dict):
            continue
        for connection in client.get("conns") or ():
            if not isinstance(connection, dict):
                continue
            found.append(
                {
                    "version": str(
                        client.get("version") or connection.get("client_version") or ""
                    ),
                    "colo": str(connection.get("colo_name") or ""),
                    "origin_ip": str(connection.get("origin_ip") or ""),
                }
            )
    return tuple(found)


@reads("cloudflare.tunnel")
def list_tunnels() -> list[dict[str, Any]]:
    """Tunnels, their ingress and their live connections.

    Connections come from each tunnel's own endpoint: the list's ``connections``
    field is removed on 2026-10-05 and is not read.
    """

    tunnels = []
    for ref in cloudflare_api.cloudflare_api_refs():
        account = _cloudflare_account(ref)
        for tunnel in _cloudflare_account_list(ref, "/cfd_tunnel?is_deleted=false"):
            tunnel_id = str(tunnel.get("id", ""))
            base = f"/accounts/{account}/cfd_tunnel/{tunnel_id}"
            record: dict[str, Any] = {
                "connection_ref": ref,
                "account_id": account,
                "id": tunnel_id,
                "name": tunnel.get("name") or "",
                "status": tunnel.get("status") or "",
                "created_at": tunnel.get("created_at") or "",
                "conns_active_at": tunnel.get("conns_active_at") or "",
            }
            scope = {"scope": record["name"], "connection_ref": ref}
            try:
                config = cloudflare_api.cloudflare_api_result(f"{base}/configurations", ref)
            except (ProviderError, OSError, ValueError) as exc:
                refuse_part("configuration", exc, **scope)
            else:
                record["config_source"] = str((config or {}).get("source") or "")
                record["ingress"] = _tunnel_ingress(config)
            try:
                clients = cloudflare_api.cloudflare_api_result(f"{base}/connections", ref)
            except (ProviderError, OSError, ValueError) as exc:
                refuse_part("connections", exc, **scope)
            else:
                record["connections"] = _tunnel_connections(clients)
            tunnels.append(record)
    return tunnels


def _earliest_expiry(pack: dict[str, Any]) -> str:
    dates = sorted(
        str(certificate.get("expires_on"))
        for certificate in pack.get("certificates") or ()
        if isinstance(certificate, dict) and certificate.get("expires_on")
    )
    return dates[0] if dates else ""


@reads("cloudflare.edge_certificate")
def list_edge_certificates() -> list[dict[str, Any]]:
    """Certificate packs on every zone the account credential can see.

    A zone whose packs are refused is a refused part on that zone; every zone
    refused is a refused read and raises.
    """

    packs: list[dict[str, Any]] = []
    for ref in cloudflare_api.cloudflare_api_refs():
        zones = [zone for zone in cloudflare_api.cloudflare_api_zones(ref) if zone.get("name")]
        refused: list[ProviderError] = []
        for zone in zones:
            name = str(zone["name"]).strip().lower().rstrip(".")
            account = str((zone.get("account") or {}).get("id") or "")
            try:
                listed = cloudflare_api.cloudflare_api_list(
                    f"/zones/{zone.get('id', '')}/ssl/certificate_packs?status=all",
                    ref,
                    per_page=50,
                )
            except (ProviderError, OSError, ValueError) as exc:
                refused.append(
                    ProviderError(
                        _unread_reason(exc),
                        refusal=getattr(exc, "refusal", ""),
                        reason=getattr(exc, "reason", ""),
                    )
                )
                refuse_part("", exc, scope=name, connection_ref=ref)
                continue
            packs.extend(
                {
                    "connection_ref": ref,
                    "account_id": account,
                    "zone": name,
                    "id": pack.get("id") or "",
                    "type": pack.get("type") or "",
                    "hosts": tuple(pack.get("hosts") or ()),
                    "status": pack.get("status") or "",
                    "certificate_authority": pack.get("certificate_authority") or "",
                    "expires_on": _earliest_expiry(pack),
                }
                for pack in listed
            )
        if zones and len(refused) == len(zones):
            raise refused[0]
    return packs


@reads("cloudflare.redirect")
def list_redirects() -> list[dict[str, Any]]:
    """Redirect rules and forwarding page rules on every zone the credential sees."""

    return redirects.read(
        cloudflare_api.cloudflare_api_refs(),
        redirects.ZoneReads(
            zones=cloudflare_api.cloudflare_api_zones,
            listed=lambda path, ref: cloudflare_api.cloudflare_api_list(path, ref, per_page=50),
            result=cloudflare_api.cloudflare_api_result,
            reason=_unread_reason,
            error=ProviderError,
            refuse=refuse_part,
        ),
    )


def _cloudflare_account(connection_ref: str) -> str:
    return provider_http.snapshot_value(
        ("cloudflare-account", connection_ref),
        lambda: cloudflare_analytics.analytics_account(connection_ref),
    )


def _cloudflare_account_list(
    connection_ref: str, path: str, *, per_page: int = 100
) -> list[dict[str, Any]]:
    """One account list endpoint, read once per sweep."""

    account = _cloudflare_account(connection_ref)
    return provider_http.snapshot_value(
        ("cloudflare-account-list", connection_ref, path),
        lambda: cloudflare_api.cloudflare_api_list(
            f"/accounts/{account}{path}", connection_ref, per_page=per_page
        ),
    )
