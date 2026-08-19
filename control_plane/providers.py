"""Typed provider declarations: emit once, derive every adapter contract."""

from __future__ import annotations

import math
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import datetime, timezone
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
    """One certificate HQ issues, deploys and keeps renewed.

    Either it names a certificate the topology already describes, or it defines
    one here. Both, because the first is how every certificate got here and the
    second is the only way a new domain ever gets one: with only the reference,
    "Add TLS certificate" offered a menu of certificates that already existed
    and no way to want a different one.

    Titles and descriptions live on the model because the form is generated from
    it. Left off, every field was labelled by its own variable name, and an
    operator was asked for a "Topology ref" -- which names the field correctly
    and the question not at all.
    """

    topology_ref: str = Field(
        default="",
        pattern=r"^(pki:[a-z0-9][a-z0-9-]*)?$",
        title="Existing certificate",
        description=(
            "One your topology already describes. Leave unset to define a new "
            "one below."
        ),
    )
    certificate_name: str = Field(
        default="",
        max_length=160,
        pattern=r"^[a-z0-9][a-z0-9-]*$|^$",
        title="New certificate name",
        description="Lowercase, no spaces. Names the certificate's own lineage.",
    )
    domains: list[str] = Field(
        default_factory=list,
        title="Names it should cover",
        description=(
            "One per line. Wildcards are fine. Every zone must be one the "
            "Cloudflare token can edit, or issuance fails before it starts."
        ),
    )
    install_on: list[str] = Field(
        default_factory=list,
        title="Install it on",
        description=(
            "Where the issued certificate gets deployed. Settings for each are "
            "taken from how that target already serves your other certificates."
        ),
    )

    renewal_window_days: int = Field(
        default=30,
        ge=1,
        le=60,
        title="Renew when this many days remain",
        description=(
            "HQ renews automatically — this only decides how early. It also "
            "renews immediately if a consumer is found serving the wrong "
            "certificate."
        ),
    )

    @model_validator(mode="after")
    def one_shape_or_the_other(self):
        defining = bool(self.certificate_name or self.domains or self.install_on)
        if self.topology_ref and defining:
            raise ValueError(
                "Name an existing certificate or define a new one, not both."
            )
        if not self.topology_ref and not defining:
            raise ValueError(
                "Choose an existing certificate, or give a name, its domains "
                "and where to install it."
            )
        if defining:
            missing = [
                label
                for label, value in (
                    ("a name", self.certificate_name),
                    ("its domains", self.domains),
                    ("somewhere to install it", self.install_on),
                )
                if not value
            ]
            if missing:
                raise ValueError("A new certificate still needs " + ", ".join(missing) + ".")
        return self


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
    domain_names: list[str] = Field(
        min_length=1,
        title="Hostnames",
        description="One per line. Every name this proxy should answer for.",
    )
    forward_scheme: Literal["http", "https"] = Field(
        title="Reach it over",
        description="How the proxy talks to your service, not how visitors do.",
    )
    forward_host: str = Field(
        min_length=1, max_length=255,
        title="Send traffic to",
        description="The address of the service itself, usually an internal IP.",
    )
    forward_port: int = Field(ge=1, le=65535, title="Port")
    certificate_resource: str = Field(
        default="",
        title="Certificate",
        description=(
            "Which certificate secures these names. Required when forcing "
            "HTTPS, which Nginx Proxy Manager cannot do without one."
        ),
    )
    force_ssl: bool = Field(
        default=True,
        title="Force HTTPS",
        description="Redirect anyone arriving over plain HTTP.",
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
        default=0, ge=0,
        title="Access list",
        description="An Nginx Proxy Manager access list id. 0 means none.",
    )
    advanced_config: str = Field(
        default="",
        title="Extra nginx configuration",
        description="Passed through as-is. Leave blank unless you need it.",
    )
    # Settings the reconciler used to assert rather than read. It sends the
    # whole proxy-host object on every pass, so a field absent from this model
    # was not left alone -- it was overwritten with a constant. HSTS was pinned
    # off, which meant turning it on in Nginx Proxy Manager survived until the
    # next reconciliation and then silently switched itself back off.
    hsts_enabled: bool = False
    hsts_subdomains: bool = False
    trust_forwarded_proto: bool = False
    # Whether the host serves at all. Named apart from ``ManagedResource.enabled``
    # deliberately: that one decides whether HQ reconciles this declaration,
    # this one is a property of the declaration itself. Collapsing them would
    # mean pausing HQ's management of a host also took the host down.
    serving: bool = True


class ResolvedNPMProxyHostSpec(NPMProxyHostSpec):
    certificate_id: int | None = Field(default=None, ge=1)


class AdGuardRewriteSpec(ProviderModel):
    domain: str = Field(
        min_length=1,
        max_length=253,
        title="Hostname",
        description="The name that should resolve on your network.",
    )
    answer: str = Field(
        min_length=1,
        max_length=253,
        title="Points at",
        description="The address this hostname resolves to, usually an IP.",
    )


class CloudflareDNSRecordSpec(ProviderModel):
    zone: str = Field(
        min_length=1, max_length=253,
        title="Zone",
        description="The domain this record belongs to, e.g. example.com.",
    )
    name: str = Field(
        min_length=1, max_length=253,
        title="Hostname",
        description="The full name being published, e.g. app.example.com.",
    )
    record_type: Literal["A", "AAAA", "CNAME", "TXT"] = Field(title="Record type")
    content: str = Field(
        min_length=1,
        title="Points at",
        description="An IP for A and AAAA, a hostname for CNAME, text for TXT.",
    )
    proxied: bool = Field(
        default=False,
        title="Proxy through Cloudflare",
        description="Hides the origin address and puts Cloudflare in the path.",
    )
    ttl: int = Field(
        default=1, ge=1, le=86400,
        title="TTL",
        description="Seconds resolvers may cache this. 1 means automatic.",
    )


@dataclass(frozen=True)
class ProviderSpec:
    kind: str
    # What this is called in a sentence, and what it does in one line. Both are
    # read by people: "adguard.rewrite" is the identifier, not the name, and a
    # page that offers it as a choice has to say what choosing it means.
    summary: str
    spec_type: type
    resolved_type: type | None = None
    resolver: Callable[[dict[str, Any], "ProviderResolutionContext"], dict[str, Any]] | None = None
    destructive: bool = False
    public_effect: bool = False
    # Declared after the positional fields, and always passed by keyword: the
    # existing entries pass resolved_type and resolver positionally, so a new
    # field inserted above them silently rebinds both.
    label: str = ""

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
    # The inverse of ``hostnames``: the spec fields that follow from being told
    # a hostname. Onboarding a service asks for the name once and seeds every
    # provider that declares a facet for it, so the operator types it once
    # rather than once per resource -- and a provider added later joins that
    # flow by saying which of its fields the name fills in.
    seed: Callable[[str], dict[str, Any]] | None = None
    # Fields that are routine tuning rather than part of the question being
    # asked. Split on required-ness instead, a spec whose validity comes from a
    # cross-field rule has no required fields at all, and its form rendered
    # empty. Required-ness describes the model; this describes the conversation,
    # and only the provider knows which of its own knobs are which.
    advanced_fields: tuple[str, ...] = ()
    # Fields that are optional to the model but unanswerable-by-default when
    # the record does not exist yet. An NPM proxy keeps whatever certificate it
    # already has when this is blank, which is a sensible default for an edit
    # and a guaranteed failure on create: the reconciler refuses to create an
    # HTTPS host with no certificate to bind, a minute later, in a job result.
    required_on_create: tuple[str, ...] = ()
    # ``module:attribute`` returning ``{field: ((value, label), ...)}`` for the
    # fields whose valid answers are a matter of live data rather than of type.
    # A topology reference is the case that forced it: rendered from the
    # annotation alone it is a blank text box that only works if you already
    # know the exact slug to type into it, which is not a form, it is a quiz.
    #
    # Late-bound as a string, the same way domains reference their providers, so
    # this module keeps declaring and stays free of database access.
    choices: str = ""
    # Turns a record the provider already holds into the spec that would
    # reproduce it. This is what makes adoption safe: the declaration starts out
    # equal to the world, so the first reconciliation after adopting changes
    # nothing. Built from the same field set the reconciler sends, so a setting
    # HQ can express is a setting adoption captures.
    from_record: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    # What this resource actually does, as (label, desired, observed) rows.
    # A service page showed "Declared" in the largest type on the card while the
    # row beneath it held `answer: 10.0.0.10` -- the least useful fact rendered
    # loudest, and the useful one not rendered at all. Desired and observed sit
    # side by side because the interesting case is when they differ, and either
    # may be blank: a certificate has no authored expiry, only a found one.
    readout: Callable[
        [dict[str, Any], dict[str, Any]], tuple[tuple[str, str, str], ...]
    ] | None = None

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


def _tls_consumer_profiles(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """How each target is configured to receive a certificate, learned from use.

    A Caddy host wants its certificate in a particular directory; a cPanel
    account is reached through a particular connection. Those are properties of
    the target, not of any one certificate, and the topology already states them
    on every ``consumes`` dependency. Reading them back means defining a new
    certificate does not ask an operator to restate deployment mechanics they
    have already described once and would only get wrong from memory.
    """

    profiles: dict[str, dict[str, Any]] = {}
    for dependency in payload.get("dependencies", ()):
        if dependency.get("relation") != "consumes":
            continue
        attributes = dict(dependency.get("attributes") or {})
        connection_ref = attributes.get("connection_ref")
        if not connection_ref or "kind" not in attributes:
            continue
        profiles.setdefault(
            connection_ref, {**attributes, "from": dependency.get("from", "")}
        )
    return profiles


def _resolve_inline_tls(
    authored: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    profiles = _tls_consumer_profiles(payload)
    consumers = []
    for connection_ref in authored["install_on"]:
        profile = profiles.get(connection_ref)
        if profile is None:
            raise ValueError(
                f"Nothing in the topology describes how {connection_ref!r} "
                "receives a certificate, so HQ cannot install one there."
            )
        consumer = {
            "topology_ref": profile["from"],
            "kind": profile["kind"],
            "connection_ref": connection_ref,
            # Named after this certificate rather than the one the profile was
            # learned from, or two certificates would collide at the target.
            "name": f"{authored['certificate_name']}-{profile['kind']}",
            "verify_domains": [],
        }
        if profile["kind"] == "caddy":
            consumer["certificate_directory"] = profile["certificate_directory"]
        elif profile["kind"] == "npm":
            consumer["discover_covered_hosts"] = profile.get(
                "discover_covered_hosts", False
            )
        elif profile["kind"] == "cpanel":
            # Only the names this certificate actually covers; the profile's own
            # install domains belong to the certificate it was learned from.
            covered = set(authored["domains"])
            consumer["install_domains"] = [
                domain
                for domain in authored["domains"]
                if certificate_covers(domain, covered) and "*" not in domain
            ]
            if not consumer["install_domains"]:
                raise ValueError(
                    "A cPanel target needs at least one non-wildcard name to "
                    "install against."
                )
        consumers.append(consumer)
    return {
        "certificate_name": authored["certificate_name"],
        "domains": authored["domains"],
        "consumers": consumers,
        "renewal_window_days": authored["renewal_window_days"],
    }


def _resolve_tls(
    authored: dict[str, Any], context: ProviderResolutionContext
) -> dict[str, Any]:
    payload = context.topology
    topology_ref = authored["topology_ref"]
    if payload is None:
        raise ValueError("TLS resolution requires the trusted topology snapshot.")
    if not topology_ref:
        return _resolve_inline_tls(authored, payload)
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


def _rewrite_readout(
    spec: dict[str, Any], status: dict[str, Any]
) -> tuple[tuple[str, str, str], ...]:
    return (("Answers with", spec.get("answer", ""), status.get("answer", "")),)


def _proxy_readout(
    spec: dict[str, Any], status: dict[str, Any]
) -> tuple[tuple[str, str, str], ...]:
    desired = (
        f"{spec.get('forward_scheme', '')}://{spec.get('forward_host', '')}"
        f":{spec.get('forward_port', '')}"
    )
    return (
        ("Forwards to", desired, status.get("forward", "")),
        ("TLS", "forced" if spec.get("force_ssl") else "optional", ""),
    )


def _expiry(stamp: str) -> str:
    """An expiry a person can act on: the date, and how long that leaves.

    The raw ISO timestamp is what the provider reports and the wrong thing to
    print. "2026-10-23T22:00:38+00:00" has to be read and subtracted from today
    before it means anything, and the number it resolves to -- how many days are
    left -- is the entire reason anyone looks at it.
    """

    try:
        expires = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return stamp or ""
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    days = math.ceil((expires - datetime.now(timezone.utc)).total_seconds() / 86400)
    if days < 0:
        return f"{expires:%-d %b %Y} — expired"
    return f"{expires:%-d %b %Y} · {days} day{'' if days == 1 else 's'}"


def _certificate_readout(
    spec: dict[str, Any], status: dict[str, Any]
) -> tuple[tuple[str, str, str], ...]:
    verified = status.get("verified_domains") or ()
    return (
        ("Issuer", "", status.get("issuer", "")),
        ("Expires", "", _expiry(status.get("not_after", ""))),
        ("Verified names", "", str(len(verified)) if verified else ""),
    )


def _dns_record_readout(
    spec: dict[str, Any], status: dict[str, Any]
) -> tuple[tuple[str, str, str], ...]:
    desired = f"{spec.get('record_type', '')} {spec.get('content', '')}".strip()
    return (("Record", desired, status.get("content", "")),)


def _rewrite_from_record(record: dict[str, Any]) -> dict[str, Any]:
    return {"domain": record["domain"], "answer": record["answer"]}


def _proxy_from_record(record: dict[str, Any]) -> dict[str, Any]:
    """An NPM proxy host, as the spec that would reproduce it exactly.

    ``certificate_resource`` is deliberately blank: it names an HQ resource, and
    the provider has only a numeric certificate id which HQ may not manage. The
    reconciler keeps whatever certificate the host already has when this is
    empty, so adopting does not detach one.
    """

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


def _rewrite_seed(hostname: str) -> dict[str, Any]:
    return {"domain": hostname}


def _proxy_seed(hostname: str) -> dict[str, Any]:
    return {"domain_names": [hostname]}


def _dns_record_seed(hostname: str) -> dict[str, Any]:
    # The registrable domain, guessed from the last two labels. A seed, not a
    # decision: it is offered in an editable field because a zone is not always
    # the last two labels, and being wrong here is visible and one keystroke to
    # correct.
    labels = hostname.split(".")
    zone = ".".join(labels[-2:]) if len(labels) > 2 else hostname
    return {"name": hostname, "zone": zone}


_PROVIDERS = (
    ProviderSpec(
        "tls.certificate",
        "Issues a certificate from Let's Encrypt and keeps it renewed and "
        "installed on everything that serves these names. Nothing to upload.",
        TLSCertificateSpec,
        ResolvedTLSCertificateSpec,
        _resolve_tls,
        label="TLS certificate",
        choices="application.provider_choices:certificate_choices",
        advanced_fields=("renewal_window_days",),
        facet="certificate",
        readout=_certificate_readout,
        hostnames=_certificate_hostnames,
        covers=True,
    ),
    ProviderSpec(
        "npm.proxy_host",
        "Sends a hostname to something running on your network, over HTTPS. "
        "Created in Nginx Proxy Manager if it is not there yet.",
        NPMProxyHostSpec,
        ResolvedNPMProxyHostSpec,
        _resolve_npm,
        label="Proxy host",
        choices="application.provider_choices:proxy_choices",
        required_on_create=("certificate_resource",),
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
        hostnames=_proxy_hostnames,
        origin=_proxy_origin,
        seed=_proxy_seed,
        from_record=_proxy_from_record,
        readout=_proxy_readout,
    ),
    ProviderSpec(
        "adguard.rewrite",
        "Makes a hostname resolve to an IP on your network. Created in AdGuard "
        "if it is not there yet.",
        AdGuardRewriteSpec,
        label="Internal DNS record",
        facet="dns",
        hostnames=_rewrite_hostnames,
        seed=_rewrite_seed,
        from_record=_rewrite_from_record,
        readout=_rewrite_readout,
    ),
    ProviderSpec(
        "cloudflare.dns_record",
        "A DNS record anyone on the internet can look up.",
        CloudflareDNSRecordSpec,
        label="Public DNS record",
        advanced_fields=("proxied", "ttl"),
        public_effect=True,
        facet="dns",
        hostnames=_dns_record_hostnames,
        seed=_dns_record_seed,
        readout=_dns_record_readout,
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
                "label": provider.label or provider.kind,
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
