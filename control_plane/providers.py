"""Typed provider declarations: emit once, derive every adapter contract."""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator


class ProviderModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ControllerVerification(ProviderModel):
    timeout_seconds: int = Field(ge=1, le=3600)
    interval_seconds: int = Field(ge=1, le=300)

    @model_validator(mode="after")
    def interval_fits_timeout(self):
        if self.interval_seconds > self.timeout_seconds:
            raise ValueError("verification interval must not exceed its timeout")
        return self


class ControllerActionPolicy(ProviderModel):
    mode: Literal["apply", "locked"]
    automatic: bool = False
    reason: str = ""
    verification: ControllerVerification | None = None

    @model_validator(mode="after")
    def validate_mode(self):
        if self.mode == "locked" and self.automatic:
            raise ValueError("locked controller actions cannot be automatic")
        if self.mode == "locked" and not self.reason:
            raise ValueError("locked controller actions require a reason")
        return self


class ControllerProviderCapability(ProviderModel):
    actions: dict[str, ControllerActionPolicy] = Field(min_length=1)


class ControllerCapabilityRegistry(ProviderModel):
    schema_version: Literal[1]
    controller_id: str = Field(min_length=1, max_length=160)
    capabilities: dict[str, ControllerProviderCapability]


class TLSConsumerBase(ProviderModel):
    topology_ref: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=160)
    verify_domains: list[str] = Field(default_factory=list)


class CaddyTLSConsumer(TLSConsumerBase):
    kind: Literal["caddy"]
    connection_ref: str = Field(min_length=1, max_length=160)
    certificate_directory: str = Field(min_length=1, max_length=500)


class NPMTLSConsumer(TLSConsumerBase):
    kind: Literal["npm"]
    connection_ref: str = Field(min_length=1, max_length=160)
    discover_covered_hosts: bool = False


class CPanelTLSConsumer(TLSConsumerBase):
    kind: Literal["cpanel"]
    connection_ref: str = Field(min_length=1, max_length=160)
    install_domains: list[str] = Field(min_length=1)


TLSConsumer = Annotated[
    CaddyTLSConsumer | NPMTLSConsumer | CPanelTLSConsumer,
    Field(discriminator="kind"),
]


class TLSCertificateSpec(ProviderModel):
    topology_ref: str = Field(pattern=r"^pki:[a-z0-9][a-z0-9-]*$")
    renewal_window_days: int = Field(default=30, ge=1, le=60)


class ResolvedTLSCertificateSpec(ProviderModel):
    certificate_name: str = Field(min_length=1, max_length=160)
    domains: list[str] = Field(min_length=1)
    consumers: list[TLSConsumer] = Field(min_length=1)
    renewal_window_days: int = Field(default=30, ge=1, le=60)

    @field_validator("domains")
    @classmethod
    def normalize_domains(cls, domains: list[str]) -> list[str]:
        normalized = [domain.strip().lower().rstrip(".") for domain in domains]
        if any(not domain or " " in domain for domain in normalized):
            raise ValueError("Certificate domains must be non-empty DNS names.")
        if len(normalized) != len(set(normalized)):
            raise ValueError("Certificate domains must be unique.")
        return normalized

    @model_validator(mode="after")
    def validate_consumers(self):
        identities = [(consumer.kind, consumer.name) for consumer in self.consumers]
        if len(identities) != len(set(identities)):
            raise ValueError("TLS consumer kind/name pairs must be unique.")

        covered = set(self.domains)
        for consumer in self.consumers:
            if not isinstance(consumer, CPanelTLSConsumer):
                continue
            uncovered = [
                domain
                for domain in consumer.install_domains
                if not certificate_covers(domain, covered)
            ]
            if uncovered:
                raise ValueError(
                    "cPanel install domains must be present in certificate domains: "
                    + ", ".join(uncovered)
                )
        return self


def certificate_covers(domain: str, names: AbstractSet[str]) -> bool:
    """Whether a set of declared names, wildcards included, answers for one name.

    Written for certificates and used by anything that has to ask the same
    question -- the service view matches a hostname against a certificate's
    names exactly this way, and a second implementation of wildcard matching is
    a second chance to get it subtly wrong.
    """

    normalized = domain.lower().rstrip(".")
    if normalized in names:
        return True
    _, separator, parent = normalized.partition(".")
    return bool(separator and f"*.{parent}" in names)


# The facets a service is assembled from, in the order a request meets them: a
# name has to resolve, something has to answer for it, and the TLS it answers
# with has to cover it.
#
# Declared here beside the providers rather than wherever services are composed,
# because a provider names the facet it supplies. A provider added later joins
# the service view by declaring one, and nothing else holds a list of what can
# participate.
SERVICE_FACETS: tuple[tuple[str, str], ...] = (
    ("dns", "DNS"),
    ("proxy", "Ingress"),
    ("certificate", "Certificate"),
)
SERVICE_FACET_IDS = frozenset(facet for facet, _ in SERVICE_FACETS)


class NPMProxyHostSpec(ProviderModel):
    domain_names: list[str] = Field(min_length=1)
    forward_scheme: Literal["http", "https"]
    forward_host: str = Field(min_length=1, max_length=255)
    forward_port: int = Field(ge=1, le=65535)
    certificate_resource: str = ""
    force_ssl: bool = True
    http2: bool = True
    websocket: bool = False
    caching_enabled: bool = False
    block_exploits: bool = True
    access_list_id: int = Field(default=0, ge=0)
    advanced_config: str = ""


class ResolvedNPMProxyHostSpec(NPMProxyHostSpec):
    certificate_id: int | None = Field(default=None, ge=1)


class AdGuardRewriteSpec(ProviderModel):
    domain: str = Field(min_length=1, max_length=253)
    answer: str = Field(min_length=1, max_length=253)


class CloudflareDNSRecordSpec(ProviderModel):
    zone: str = Field(min_length=1, max_length=253)
    name: str = Field(min_length=1, max_length=253)
    record_type: Literal["A", "AAAA", "CNAME", "TXT"]
    content: str = Field(min_length=1)
    proxied: bool = False
    ttl: int = Field(default=1, ge=1, le=86400)


@dataclass(frozen=True)
class ProviderSpec:
    kind: str
    summary: str
    spec_type: type
    resolved_type: type | None = None
    resolver: Callable[[dict[str, Any], "ProviderResolutionContext"], dict[str, Any]] | None = None
    destructive: bool = False
    public_effect: bool = False

    # ----- Service participation ---------------------------------------------
    #
    # A service is a hostname and everything that has to be true for it to
    # answer. A provider joins that view by naming the facet it supplies and
    # saying how to read the hostnames out of a *resolved* spec. A provider that
    # names neither simply does not appear there, so nothing needs an exclusion
    # list to keep it out.
    facet: str = ""
    hostnames: Callable[[dict[str, Any]], tuple[str, ...]] | None = None
    # Whether this provider *covers* hostnames rather than declaring them.
    # Declaring brings a service into existence -- something has to name it
    # before it is a thing at all. Covering answers for a set that may include
    # wildcards, so it attaches to services declared elsewhere and never invents
    # one: treated as a declaration, a wildcard certificate would conjure a
    # service literally called "*.example.com".
    covers: bool = False
    # Where a request for these hostnames is finally served, as "host:port".
    # Only an ingress provider has one.
    origin: Callable[[dict[str, Any]], str] | None = None

    def __post_init__(self) -> None:
        if self.facet and self.facet not in SERVICE_FACET_IDS:
            raise ValueError(
                f"Provider {self.kind!r} declares unknown service facet "
                f"{self.facet!r}; expected one of {sorted(SERVICE_FACET_IDS)}."
            )
        if (self.covers or self.hostnames or self.origin) and not self.facet:
            raise ValueError(
                f"Provider {self.kind!r} describes hostnames but names no "
                "service facet, so nothing would ever read them."
            )

    def schema(self) -> dict[str, Any]:
        return TypeAdapter(self.spec_type).json_schema()

    def validate(self, payload: dict[str, Any]):
        return TypeAdapter(self.spec_type).validate_python(payload)


@dataclass(frozen=True)
class ProviderResolutionContext:
    topology: dict[str, Any] | None = None
    resource_status: Callable[[str, str], dict[str, Any] | None] | None = None


def _resolve_tls(
    authored: dict[str, Any], context: ProviderResolutionContext
) -> dict[str, Any]:
    payload = context.topology
    topology_ref = authored["topology_ref"]
    if payload is None:
        raise ValueError("TLS resolution requires the trusted topology snapshot.")
    certificate_id = topology_ref.removeprefix("pki:")
    certificate = next(
        (entry for entry in payload["pki"] if entry["id"] == certificate_id), None
    )
    if certificate is None:
        raise ValueError(f"Topology certificate {topology_ref!r} was not found.")
    consumers = [
        {"topology_ref": dependency["from"], **dependency.get("attributes", {})}
        for dependency in payload["dependencies"]
        if dependency.get("relation") == "consumes"
        and dependency.get("to") == topology_ref
    ]
    if not consumers:
        raise ValueError(f"Topology certificate {topology_ref!r} has no consumers.")
    return {
        "certificate_name": certificate.get("certificate_name", certificate_id),
        "domains": certificate.get("domains", []),
        "consumers": consumers,
        "renewal_window_days": authored["renewal_window_days"],
    }


def _resolve_npm(
    authored: dict[str, Any], context: ProviderResolutionContext
) -> dict[str, Any]:
    certificate_id = None
    resource_key = authored.get("certificate_resource")
    if resource_key and context.resource_status:
        status = context.resource_status(resource_key, "tls.certificate")
        certificate_id = status.get("npm_certificate_id") if status else None
    return {**authored, "certificate_id": certificate_id}


# Each reads a *resolved* spec, which is why a certificate can answer at all:
# authored, it declares only a topology reference, and the names it covers exist
# solely on the far side of resolution.


def _certificate_hostnames(spec: dict[str, Any]) -> tuple[str, ...]:
    return tuple(spec.get("domains", ()))


def _proxy_hostnames(spec: dict[str, Any]) -> tuple[str, ...]:
    return tuple(spec["domain_names"])


def _proxy_origin(spec: dict[str, Any]) -> str:
    return f"{spec['forward_host']}:{spec['forward_port']}"


def _rewrite_hostnames(spec: dict[str, Any]) -> tuple[str, ...]:
    return (spec["domain"],)


def _dns_record_hostnames(spec: dict[str, Any]) -> tuple[str, ...]:
    # A TXT record carries policy -- an SPF entry, a validation challenge -- not
    # a service. Naming one would put a hostname on the board that nothing is
    # expected to serve, and then permanently report it as unserved.
    return () if spec["record_type"] == "TXT" else (spec["name"],)


_PROVIDERS = (
    ProviderSpec(
        "tls.certificate",
        "Renew and distribute a certificate to declared TLS consumers.",
        TLSCertificateSpec,
        ResolvedTLSCertificateSpec,
        _resolve_tls,
        facet="certificate",
        hostnames=_certificate_hostnames,
        covers=True,
    ),
    ProviderSpec(
        "npm.proxy_host",
        "Reconcile an Nginx Proxy Manager proxy host.",
        NPMProxyHostSpec,
        ResolvedNPMProxyHostSpec,
        _resolve_npm,
        facet="proxy",
        hostnames=_proxy_hostnames,
        origin=_proxy_origin,
    ),
    ProviderSpec(
        "adguard.rewrite",
        "Reconcile an internal AdGuard DNS rewrite.",
        AdGuardRewriteSpec,
        facet="dns",
        hostnames=_rewrite_hostnames,
    ),
    ProviderSpec(
        "cloudflare.dns_record",
        "Reconcile a public Cloudflare DNS record.",
        CloudflareDNSRecordSpec,
        public_effect=True,
        facet="dns",
        hostnames=_dns_record_hostnames,
    ),
)

PROVIDERS = {provider.kind: provider for provider in _PROVIDERS}


@lru_cache(maxsize=1)
def controller_capability_registry() -> ControllerCapabilityRegistry:
    registry_path = (
        Path(__file__).resolve().parents[1]
        / "config"
        / "controller-capabilities.json"
    )
    registry = ControllerCapabilityRegistry.model_validate_json(
        registry_path.read_text(encoding="utf-8")
    )
    if set(registry.capabilities) != set(PROVIDERS):
        raise ValueError(
            "Controller capability registry must declare every provider exactly once."
        )
    return registry


def controller_capabilities() -> dict[str, Any]:
    """Return the one validated, JSON-safe controller contract."""

    return controller_capability_registry().model_dump(mode="json")


def enabled_controller_actions(*, automatic_only: bool = False) -> tuple[tuple[str, str], ...]:
    registry = controller_capability_registry()
    return tuple(
        sorted(
            (kind, action)
            for kind, capability in registry.capabilities.items()
            for action, policy in capability.actions.items()
            if policy.mode == "apply" and (policy.automatic or not automatic_only)
        )
    )


def controller_action_policy(kind: str, action: str) -> tuple[bool, str]:
    capability = controller_capability_registry().capabilities.get(kind)
    policy = capability.actions.get(action) if capability else None
    if not policy:
        return False, f"The controller does not implement {action!r} for {kind!r}."
    if policy.mode != "apply":
        return False, policy.reason or "Controller capability is locked."
    return True, "Controller capability is active."


def describe_providers() -> dict[str, Any]:
    capabilities = controller_capabilities()["capabilities"]
    return {
        "schema_version": 1,
        "controller": {
            "id": controller_capabilities()["controller_id"],
            "capabilities": capabilities,
        },
        "providers": [
            {
                "kind": provider.kind,
                "summary": provider.summary,
                "destructive": provider.destructive,
                "public_effect": provider.public_effect,
                "controller": capabilities[provider.kind],
                "spec_schema": provider.schema(),
            }
            for provider in _PROVIDERS
        ],
    }


def validate_spec(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        provider = PROVIDERS[kind]
    except KeyError as exc:
        raise ValueError(f"Unknown infrastructure resource kind {kind!r}.") from exc
    validated = provider.validate(payload)
    return TypeAdapter(provider.spec_type).dump_python(validated, mode="json")


def validate_resolved_certificate(payload: dict[str, Any]) -> dict[str, Any]:
    validated = TypeAdapter(ResolvedTLSCertificateSpec).validate_python(payload)
    return TypeAdapter(ResolvedTLSCertificateSpec).dump_python(validated, mode="json")


def resolve_provider_spec(
    kind: str,
    payload: dict[str, Any],
    *,
    context: ProviderResolutionContext,
) -> dict[str, Any]:
    """Validate authored state, resolve references, then validate runtime state."""

    provider = PROVIDERS[kind]
    authored = validate_spec(kind, payload)
    resolved = provider.resolver(authored, context) if provider.resolver else authored
    resolved_type = provider.resolved_type or provider.spec_type
    value = TypeAdapter(resolved_type).validate_python(resolved)
    return TypeAdapter(resolved_type).dump_python(value, mode="json")
