"""Readings from the Cloudflare account credential.

Each record names only what HQ keeps. Build configuration, environment values,
SaaS and SCIM settings, client IDs and secrets are never named, so the schema
drops them. ``unread`` says which part of a record could not be read.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..certificate_authorities import authority_name
from ..consoles import cloudflare_dashboard, cloudflare_zero_trust
from ..names import normalized_hostname
from .contract import ObservationRecord, ObservationSpec


def _present(*values: Any) -> tuple[str, ...]:
    found: list[str] = []
    for value in values:
        items = value if isinstance(value, (list, tuple)) else (value,)
        for item in items:
            text = normalized_hostname(item)
            if text and text not in found:
                found.append(text)
    return tuple(found)


class PagesProjectRecord(ObservationRecord):
    connection_ref: str = ""
    account_id: str = ""
    name: str
    subdomain: str = ""
    domains: tuple[str, ...] = ()
    production_branch: str = ""
    # The latest production deployment.
    deployment_id: str = ""
    deployment_commit: str = ""
    deployment_created_on: str = ""


class D1DatabaseRecord(ObservationRecord):
    connection_ref: str = ""
    account_id: str = ""
    name: str
    uuid: str
    created_at: str = ""
    version: str = ""
    file_size: int | None = None
    unread: str = ""


class AccessPolicyRecord(ObservationRecord):
    id: str = ""
    name: str = ""


class AccessAppRecord(ObservationRecord):
    connection_ref: str = ""
    account_id: str = ""
    id: str
    name: str = ""
    type: str = ""
    domain: str = ""
    # Hostnames only: a public destination's path and a private CIDR are left out.
    destinations: tuple[str, ...] = ()
    session_duration: str = ""
    policies: tuple[AccessPolicyRecord, ...] = ()


class AccessAppRef(ObservationRecord):
    id: str = ""
    name: str = ""


class AccessServiceTokenRecord(ObservationRecord):
    connection_ref: str = ""
    id: str
    name: str = ""
    expires_at: str = ""
    created_at: str = ""
    # Applications with a policy whose include rules admit this token.
    apps: tuple[AccessAppRef, ...] = ()
    unread: str = ""


class TunnelIngressRecord(ObservationRecord):
    hostname: str
    service: str = ""


class TunnelConnectionRecord(ObservationRecord):
    version: str = ""
    colo: str = ""
    origin_ip: str = ""


class TunnelRecord(ObservationRecord):
    connection_ref: str = ""
    account_id: str = ""
    id: str
    name: str = ""
    status: str = ""
    created_at: str = ""
    conns_active_at: str = ""
    # "cloudflare" when the ingress is managed remotely, "local" when it lives
    # in the connector's own file and the API does not hold it.
    config_source: str = ""
    ingress: tuple[TunnelIngressRecord, ...] = ()
    connections: tuple[TunnelConnectionRecord, ...] = ()
    unread: str = ""


class EdgeCertificateRecord(ObservationRecord):
    connection_ref: str = ""
    account_id: str = ""
    zone: str
    id: str = ""
    type: str = ""
    hosts: tuple[str, ...] = ()
    status: str = ""
    certificate_authority: str = ""
    # The earliest expiry among the pack's certificates.
    expires_on: str = ""
    unread: str = ""


def _ingress_hosts(record: Mapping[str, Any]) -> tuple[str, ...]:
    return _present([entry.get("hostname") for entry in record.get("ingress") or ()])


def _origin_addresses(record: Mapping[str, Any]) -> tuple[str, ...]:
    return _present(
        [entry.get("origin_ip") for entry in record.get("connections") or ()]
    )


OBSERVATIONS: tuple[ObservationSpec, ...] = (
    ObservationSpec(
        "cloudflare.pages_project",
        "cloudflare_api",
        "Pages project",
        PagesProjectRecord,
        requires=("Cloudflare Pages Read (account)",),
        hostnames=lambda record: _present(
            record.get("domains") or (), record.get("subdomain")
        ),
        title=lambda record: str(record.get("name", "")),
        relation="Served by Pages project",
        facet="runtime",
        console=lambda record: cloudflare_dashboard(
            record, "pages", "view", str(record.get("name", ""))
        ),
    ),
    ObservationSpec(
        "cloudflare.d1_database",
        "cloudflare_api",
        "D1 database",
        D1DatabaseRecord,
        requires=("D1 Read (account)",),
        title=lambda record: str(record.get("name", "")),
        relation="Backed by D1 database",
        console=lambda record: cloudflare_dashboard(
            record, "workers", "d1", "databases", str(record.get("uuid", ""))
        ),
    ),
    ObservationSpec(
        "cloudflare.access_app",
        "cloudflare_api",
        "Access application",
        AccessAppRecord,
        requires=("Access: Apps Read (account)", "Access: Policies Read (account)"),
        hostnames=lambda record: _present(
            record.get("domain", "").split("/", 1)[0],
            record.get("destinations") or (),
        ),
        title=lambda record: str(record.get("name", "")),
        relation="Behind Access",
        console=lambda record: cloudflare_zero_trust(record, "access", "apps"),
    ),
    ObservationSpec(
        "cloudflare.access_service_token",
        "cloudflare_api",
        "Access service token",
        AccessServiceTokenRecord,
        requires=("Access: Service Tokens Read (account)",),
        title=lambda record: str(record.get("name", "")),
        relation="Admitted by service token",
        expires=lambda record: str(record.get("expires_at", "")),
    ),
    ObservationSpec(
        "cloudflare.tunnel",
        "cloudflare_api",
        "Tunnel",
        TunnelRecord,
        requires=("Cloudflare Tunnel Read (account)",),
        hostnames=_ingress_hosts,
        addresses=_origin_addresses,
        title=lambda record: str(record.get("name", "")),
        relation="Published through tunnel",
        address_relation="Runs connector for tunnel",
        facet="proxy",
        console=lambda record: cloudflare_zero_trust(record, "networks", "tunnels"),
    ),
    ObservationSpec(
        "cloudflare.edge_certificate",
        "cloudflare_api",
        "Edge certificate",
        EdgeCertificateRecord,
        requires=("Zone Read (zone)", "SSL and Certificates Read (zone)"),
        hostnames=lambda record: _present(record.get("hosts") or ()),
        title=lambda record: str(record.get("zone", "")),
        relation="Covered by edge certificate",
        facet="certificate",
        fronted_by="cloudflare.dns_record",
        console=lambda record: cloudflare_dashboard(
            record, str(record.get("zone", "")), "ssl-tls", "edge-certificates"
        ),
        short_label="Edge",
        expires=lambda record: str(record.get("expires_on", "")),
        issuer=lambda record: authority_name(record.get("certificate_authority")),
    ),
)
