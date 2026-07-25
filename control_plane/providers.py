"""Typed provider declarations: emit once, derive every adapter contract."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator


class ProviderModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


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


def certificate_covers(domain: str, names: set[str]) -> bool:
    normalized = domain.lower().rstrip(".")
    if normalized in names:
        return True
    _, separator, parent = normalized.partition(".")
    return bool(separator and f"*.{parent}" in names)


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
    destructive: bool = False
    public_effect: bool = False

    def schema(self) -> dict[str, Any]:
        return TypeAdapter(self.spec_type).json_schema()

    def validate(self, payload: dict[str, Any]):
        return TypeAdapter(self.spec_type).validate_python(payload)


_PROVIDERS = (
    ProviderSpec(
        "tls.certificate",
        "Renew and distribute a certificate to declared TLS consumers.",
        TLSCertificateSpec,
    ),
    ProviderSpec(
        "npm.proxy_host",
        "Reconcile an Nginx Proxy Manager proxy host.",
        NPMProxyHostSpec,
    ),
    ProviderSpec(
        "adguard.rewrite",
        "Reconcile an internal AdGuard DNS rewrite.",
        AdGuardRewriteSpec,
    ),
    ProviderSpec(
        "cloudflare.dns_record",
        "Reconcile a public Cloudflare DNS record.",
        CloudflareDNSRecordSpec,
        public_effect=True,
    ),
)

PROVIDERS = {provider.kind: provider for provider in _PROVIDERS}


@lru_cache(maxsize=1)
def controller_capabilities() -> dict[str, Any]:
    registry_path = (
        Path(__file__).resolve().parents[1]
        / "config"
        / "controller-capabilities.json"
    )
    registry = json.loads(registry_path.read_text())
    if registry.get("schema_version") != 1:
        raise ValueError("Unsupported controller capability registry version.")
    capabilities = registry.get("capabilities")
    if not isinstance(capabilities, dict) or set(capabilities) != set(PROVIDERS):
        raise ValueError(
            "Controller capability registry must declare every provider exactly once."
        )
    for kind, capability in capabilities.items():
        actions = capability.get("actions")
        if not isinstance(actions, dict) or not actions:
            raise ValueError(f"Controller actions are required for {kind}.")
        for action, policy in actions.items():
            if policy.get("mode") not in {"apply", "locked"}:
                raise ValueError(f"Invalid controller mode for {kind}/{action}.")
    return registry


def controller_action_policy(kind: str, action: str) -> tuple[bool, str]:
    capability = controller_capabilities()["capabilities"].get(kind, {})
    policy = capability.get("actions", {}).get(action)
    if not policy:
        return False, f"The controller does not implement {action!r} for {kind!r}."
    if policy.get("mode") != "apply":
        return False, policy.get("reason") or "Controller capability is locked."
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
