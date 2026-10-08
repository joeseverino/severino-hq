"""Nginx Proxy Manager: the proxy hosts HQ declares. The controller reads and writes them."""

from typing import Any, Literal

from pydantic import Field

from hq.platform.core.network import split_host_port

from ..connection_shapes import LOGIN
from ..names import normalized_hostname
from ..provider_spec import ConnectionKind, ProviderModel, ProviderSpec, applies
from .contracts import IngressPolicy, ServedCertificate

# The corroborating headers NPM adds beside X-Forwarded-For: client, then scheme.
FORWARDING_HEADERS = ("X-Real-IP", "X-Forwarded-Scheme")


def _names(record: dict[str, Any]) -> tuple[str, ...]:
    return tuple(name for name in (normalized_hostname(item) for item in record.get("domain_names") or ()) if name)


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


class NPMProxyHostSpec(ProviderModel):
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


def _resolve(authored, context):
    resource_key = authored.get("certificate_resource")
    status = (
        context.resource_status(resource_key, ("tls.certificate", "tls.uploaded_certificate"))
        if resource_key and context.resource_status
        else None
    )
    return {
        **authored,
        "certificate_id": status.get("npm_certificate_id") if status else None,
    }


def _from_record(record):
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


def _seed(context):
    host, port = split_host_port(context.origin_address or context.origin)
    result = {"domain_names": [context.hostname]}
    if host and port.isdigit():
        result.update(forward_host=host, forward_port=int(port))
    if len(context.certificates) == 1:
        result["certificate_resource"] = context.certificates[0]
    return result


DEFINITION = ProviderSpec(
    "npm.proxy_host",
    "Forwards a hostname to a service on your network over HTTPS. HQ creates it in Nginx Proxy Manager if it does not exist.",
    NPMProxyHostSpec,
    ResolvedNPMProxyHostSpec,
    _resolve,
    actions={"reconcile": applies(automatic=True), "delete": applies()},
    label="Proxy host",
    connection_providers=("npm",),
    removal_note=lambda spec: "These names stop being served: " + ", ".join(spec.get("domain_names", ())) + ".",
    choices="hq.platform.application.provider_choices:proxy_choices",
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
    seed=_seed,
    from_record=_from_record,
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
DEFINITIONS = (DEFINITION,)

# The connection this provider's credential arrives through, beside its kinds:
# admitting the module admits both.
CONNECTIONS = {"npm": ConnectionKind("Nginx Proxy Manager", "coarse", LOGIN)}
