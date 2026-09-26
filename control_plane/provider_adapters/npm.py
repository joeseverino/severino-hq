"""Nginx Proxy Manager emits its declaration and controller surfaces together."""

from __future__ import annotations

import urllib.parse
from typing import Any, Literal

from pydantic import Field

from core.network import split_host_port

from ..names import normalized_hostname
from .contracts import (
    ControllerIntegrationAdapter,
    IngressPolicy,
    ProviderError,
    ProviderResult,
    ProviderRuntime,
    ServedCertificate,
)

# The corroborating headers NPM adds beside X-Forwarded-For: client, then scheme.
FORWARDING_HEADERS = ("X-Real-IP", "X-Forwarded-Scheme")


def _names(record: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        name
        for name in (normalized_hostname(item) for item in record.get("domain_names") or ())
        if name
    )


def ingress_policy(record: dict[str, Any]) -> IngressPolicy:
    """A proxy host record's access list, in provider-neutral terms."""

    policy = record.get("access_policy")
    if not isinstance(policy, dict):
        return IngressPolicy(_names(record), bool(record.get("access_list_id")))
    clients = policy.get("clients")
    count = policy.get("authorization_count")
    return IngressPolicy(
        hostnames=_names(record),
        restricted=bool(record.get("access_list_id")),
        rules=tuple(
            (str(rule.get("directive", "")).lower(), str(rule.get("address", "")).lower())
            for rule in (clients if isinstance(clients, list) else ())
            if isinstance(rule, dict)
        ),
        implicit_deny=policy.get("implicit_deny") is True,
        satisfy_any=policy.get("satisfy_any") is not False,
        passes_auth=policy.get("pass_auth") is not False,
        authorizations=count if isinstance(count, int) and not isinstance(count, bool) else None,
    )


def served_certificate(record: dict[str, Any]) -> ServedCertificate | None:
    """The certificate a proxy host record serves its names with, when it names one."""

    certificate = record.get("certificate") or {}
    if not isinstance(certificate, dict) or not certificate.get("name"):
        return None
    return ServedCertificate(_names(record), certificate)


def api_url(configured_url: str) -> str:
    parsed = urllib.parse.urlsplit(configured_url.rstrip("/"))
    path = parsed.path.rstrip("/")
    if not path.endswith("/api"):
        path = f"{path}/api"
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment)
    )


def url(runtime: ProviderRuntime, connection_ref: str = "") -> str:
    prefix = runtime.connection_prefix("npm", connection_ref)
    return api_url(runtime.required(prefix, "URL"))


def token(runtime: ProviderRuntime, base_url: str, connection_ref: str = "") -> str:
    prefix = runtime.connection_prefix("npm", connection_ref)

    def exchange() -> str:
        result = runtime.request(
            f"{base_url}/tokens",
            method="POST",
            payload={
                "identity": runtime.required(prefix, "USERNAME"),
                "secret": runtime.required(prefix, "PASSWORD"),
            },
        )
        value = result.get("token", "") if isinstance(result, dict) else ""
        if not value:
            raise ProviderError("NPM authentication did not return a token.")
        return value

    return runtime.snapshot_value(("npm-token", base_url, prefix), exchange)


def _session(runtime: ProviderRuntime, connection_ref: str = ""):
    base_url = url(runtime, connection_ref)
    return base_url, {
        "Authorization": f"Bearer {token(runtime, base_url, connection_ref)}"
    }


def reconcile(
    runtime: ProviderRuntime,
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    base_url, headers = _session(runtime)
    hosts = runtime.request(f"{base_url}/nginx/proxy-hosts", headers=headers)
    domains = sorted(spec["domain_names"])
    matches = [
        host for host in hosts if sorted(host.get("domain_names", [])) == domains
    ]
    if not matches:
        previous = sorted((observed or {}).get("domain_names") or ())
        if previous and previous != domains:
            matches = [
                host
                for host in hosts
                if sorted(host.get("domain_names", [])) == previous
            ]
    if len(matches) > 1:
        raise ProviderError("NPM contains duplicate proxy hosts for the domain set.")

    desired = {
        "domain_names": domains,
        "forward_scheme": spec["forward_scheme"],
        "forward_host": spec["forward_host"],
        "forward_port": spec["forward_port"],
        "caching_enabled": spec["caching_enabled"],
        "block_exploits": spec["block_exploits"],
        "allow_websocket_upgrade": spec["websocket"],
        "access_list_id": spec["access_list_id"],
        "certificate_id": spec.get("certificate_id") or 0,
        "ssl_forced": spec["force_ssl"],
        "http2_support": spec["http2"],
        "hsts_enabled": spec["hsts_enabled"],
        "hsts_subdomains": spec["hsts_subdomains"],
        "trust_forwarded_proto": spec["trust_forwarded_proto"],
        "advanced_config": spec["advanced_config"],
        "locations": [],
        "enabled": spec["serving"],
        "meta": {},
    }
    if matches:
        current = matches[0]
        if spec["force_ssl"] and not current.get("certificate_id"):
            raise ProviderError(
                "The proxy host forces HTTPS but has no certificate. Attach "
                "one, then reconcile."
            )
        if not desired["certificate_id"]:
            desired["certificate_id"] = current.get("certificate_id", 0)
        desired["locations"] = current.get("locations", [])
        desired["meta"] = current.get("meta", {})
        changed = {key: current.get(key) for key in desired} != desired
        if changed and apply:
            runtime.request(
                f"{base_url}/nginx/proxy-hosts/{current['id']}",
                method="PUT",
                headers=headers,
                payload=desired,
            )
    else:
        if spec["force_ssl"] and not desired["certificate_id"]:
            raise ProviderError(
                "An HTTPS proxy host needs an issued certificate. None is set yet."
            )
        if apply:
            runtime.request(
                f"{base_url}/nginx/proxy-hosts",
                method="POST",
                headers=headers,
                payload=desired,
            )
        changed = True
    return ProviderResult(
        changed=changed,
        status={
            "domain_names": domains,
            "forward": f"{spec['forward_scheme']}://{spec['forward_host']}:{spec['forward_port']}",
        },
        conditions=[
            runtime.condition("Ready", True, "Reconciled", "NPM proxy host is current.")
        ],
        message="NPM proxy host updated." if changed else "NPM proxy host unchanged.",
    )


def delete(
    runtime: ProviderRuntime,
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    del observed
    base_url, headers = _session(runtime)
    hosts = runtime.request(f"{base_url}/nginx/proxy-hosts", headers=headers)
    domains = sorted(spec["domain_names"])
    matches = [
        host for host in hosts if sorted(host.get("domain_names", [])) == domains
    ]
    if len(matches) > 1:
        raise ProviderError("NPM contains duplicate proxy hosts for the domain set.")
    if not matches:
        return ProviderResult(
            False,
            {"domain_names": domains, "removed": True},
            [runtime.condition("Ready", True, "Absent", "No such proxy host in NPM.")],
            "NPM proxy host was already absent.",
        )
    if apply:
        runtime.request(
            f"{base_url}/nginx/proxy-hosts/{matches[0]['id']}",
            method="DELETE",
            headers=headers,
        )
    return ProviderResult(
        True,
        {"domain_names": domains, "removed": True},
        [runtime.condition("Ready", True, "Removed", "NPM proxy host was removed.")],
        "NPM proxy host removed.",
    )


def _access_policies(runtime: ProviderRuntime, base_url: str, headers: dict[str, str]):
    try:
        records = runtime.request(
            f"{base_url}/nginx/access-lists?expand=items,clients", headers=headers
        )
    except (ProviderError, OSError, ValueError, KeyError):
        return {}
    found = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("id"), int):
            continue
        clients = [
            {
                "directive": str(client.get("directive", "")),
                "address": str(client.get("address", "")),
            }
            for client in record.get("clients") or ()
            if client.get("directive") and client.get("address")
        ]
        found[record["id"]] = {
            "name": str(record.get("name", "")),
            "satisfy_any": bool(record.get("satisfy_any", False)),
            "pass_auth": bool(record.get("pass_auth", False)),
            "authorization_count": len(record.get("items") or ()),
            "clients": clients,
            "implicit_deny": bool(clients),
        }
    return found


def _certificates(runtime: ProviderRuntime, base_url: str, headers: dict[str, str]):
    try:
        records = runtime.request(f"{base_url}/nginx/certificates", headers=headers)
    except (ProviderError, OSError, ValueError, KeyError):
        return {}
    return {
        item.get("id"): {
            "name": str(item.get("nice_name", "")),
            "domains": [str(name) for name in item.get("domain_names") or ()],
            "expires_on": str(item.get("expires_on", "")),
            "provider": str(item.get("provider", "")),
        }
        for item in records or ()
        if item.get("id")
    }


def inventory(runtime: ProviderRuntime) -> list[dict[str, Any]]:
    base_url, headers = _session(runtime)
    records = runtime.request(f"{base_url}/nginx/proxy-hosts", headers=headers)
    policies = _access_policies(runtime, base_url, headers)
    certificates = _certificates(runtime, base_url, headers)
    fields = (
        "domain_names",
        "forward_scheme",
        "forward_host",
        "forward_port",
        "ssl_forced",
        "http2_support",
        "allow_websocket_upgrade",
        "caching_enabled",
        "block_exploits",
        "access_list_id",
        "advanced_config",
        "hsts_enabled",
        "hsts_subdomains",
        "trust_forwarded_proto",
        "enabled",
    )
    return [
        {
            **{field: record.get(field) for field in fields},
            "certificate": certificates.get(record.get("certificate_id"), {}),
            "access_policy": policies.get(record.get("access_list_id")),
        }
        for record in records
        if record.get("domain_names")
    ]


def probe(runtime: ProviderRuntime, connection_ref: str) -> dict[str, Any]:
    base_url = url(runtime, connection_ref)
    token(runtime, base_url, connection_ref)
    return {"detail": "Authenticated.", "reaches": []}


def build_adapter(*, provider_model, provider_spec, applies):
    class NPMProxyHostSpec(provider_model):
        domain_names: list[str] = Field(
            min_length=1,
            title="Hostnames",
            description="One per line.",
        )
        forward_scheme: Literal["http", "https"] = Field(
            title="Reach it over",
            description="How the proxy connects to your service.",
        )
        forward_host: str = Field(
            min_length=1,
            max_length=255,
            title="Send traffic to",
            description="The service's address, usually an internal IP.",
        )
        forward_port: int = Field(ge=1, le=65535, title="Port")
        certificate_resource: str = Field(
            default="",
            title="Certificate",
            description="Secures these names. Required when Force HTTPS is on.",
        )
        force_ssl: bool = Field(
            default=True,
            title="Force HTTPS",
            description="Redirect HTTP to HTTPS.",
        )
        http2: bool = Field(default=True, title="HTTP/2")
        websocket: bool = Field(
            default=False,
            title="Allow websockets",
            description="Needed for live updates, terminals and chat.",
        )
        caching_enabled: bool = Field(default=False, title="Cache assets")
        block_exploits: bool = Field(
            default=True,
            title="Block common exploits",
            description="Nginx Proxy Manager's built-in request filtering.",
        )
        access_list_id: int = Field(
            default=0,
            ge=0,
            title="Access list",
            description="An Nginx Proxy Manager access list id. 0 means none.",
        )
        advanced_config: str = Field(
            default="",
            title="Extra nginx configuration",
            description="Passed through as-is. Usually blank.",
        )
        hsts_enabled: bool = False
        hsts_subdomains: bool = False
        trust_forwarded_proto: bool = False
        serving: bool = True

    class ResolvedNPMProxyHostSpec(NPMProxyHostSpec):
        certificate_id: int | None = Field(default=None, ge=1)

    def resolve(authored, context):
        resource_key = authored.get("certificate_resource")
        status = (
            context.resource_status(
                resource_key, ("tls.certificate", "tls.uploaded_certificate")
            )
            if resource_key and context.resource_status
            else None
        )
        return {
            **authored,
            "certificate_id": status.get("npm_certificate_id") if status else None,
        }

    def from_record(record):
        return {
            "domain_names": list(record["domain_names"]),
            "forward_scheme": record["forward_scheme"],
            "forward_host": record["forward_host"],
            "forward_port": record["forward_port"],
            "certificate_resource": "",
            "force_ssl": bool(record.get("ssl_forced")),
            "http2": bool(record.get("http2_support")),
            "websocket": bool(record.get("allow_websocket_upgrade")),
            "caching_enabled": bool(record.get("caching_enabled")),
            "block_exploits": bool(record.get("block_exploits")),
            "access_list_id": record.get("access_list_id") or 0,
            "advanced_config": record.get("advanced_config") or "",
            "hsts_enabled": bool(record.get("hsts_enabled")),
            "hsts_subdomains": bool(record.get("hsts_subdomains")),
            "trust_forwarded_proto": bool(record.get("trust_forwarded_proto")),
            "serving": bool(record.get("enabled", True)),
        }

    def seed(context):
        host, port = split_host_port(context.origin_address or context.origin)
        result = {"domain_names": [context.hostname]}
        if host and port.isdigit():
            result.update(forward_host=host, forward_port=int(port))
        if len(context.certificates) == 1:
            result["certificate_resource"] = context.certificates[0]
        return result

    definition = provider_spec(
        "npm.proxy_host",
        "Forwards a hostname to a service on your network over HTTPS. HQ creates it in Nginx Proxy Manager if it does not exist.",
        NPMProxyHostSpec,
        ResolvedNPMProxyHostSpec,
        resolve,
        actions={"reconcile": applies(automatic=True), "delete": applies()},
        label="Proxy host",
        connection_providers=("npm",),
        removal_note=lambda spec: (
            "These names stop being served: "
            + ", ".join(spec.get("domain_names", ()))
            + "."
        ),
        choices="application.provider_choices:proxy_choices",
        required_on_create=("certificate_resource",),
        unobservable_fields=("certificate_resource",),
        advanced_fields=(
            "http2",
            "websocket",
            "caching_enabled",
            "block_exploits",
            "access_list_id",
            "advanced_config",
            "hsts_enabled",
            "hsts_subdomains",
            "trust_forwarded_proto",
            "serving",
        ),
        facet="proxy",
        ingress_policy=ingress_policy,
        served_certificate=served_certificate,
        forwarding_headers=FORWARDING_HEADERS,
        hostnames=lambda spec: tuple(spec["domain_names"]),
        certificate=lambda spec: str(spec.get("certificate_resource", "") or ""),
        origin=lambda spec: f"{spec['forward_host']}:{spec['forward_port']}",
        seed=seed,
        from_record=from_record,
        sample_record={
            "domain_names": ["shop.example.com"],
            "forward_scheme": "http",
            "forward_host": "10.0.0.20",
            "forward_port": 3000,
            "ssl_forced": True,
            "http2_support": True,
            "allow_websocket_upgrade": False,
            "caching_enabled": False,
            "block_exploits": True,
            "access_list_id": 0,
            "advanced_config": "",
            "hsts_enabled": False,
            "hsts_subdomains": False,
            "trust_forwarded_proto": False,
            "enabled": True,
        },
        readout=lambda spec, status: (
            (
                "Forwards to",
                f"{spec.get('forward_scheme', '')}://{spec.get('forward_host', '')}:{spec.get('forward_port', '')}",
                status.get("forward", ""),
            ),
            ("TLS", "forced" if spec.get("force_ssl") else "optional", ""),
        ),
    )
    return ControllerIntegrationAdapter(
        definitions=(definition,),
        inventory={definition.kind: inventory},
        connection_probes={"npm": probe},
        actions={
            (definition.kind, "reconcile"): reconcile,
            (definition.kind, "delete"): delete,
        },
    )
