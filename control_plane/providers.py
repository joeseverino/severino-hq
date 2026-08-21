"""Typed provider declarations: emit once, derive every adapter contract."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Callable, Literal, get_args

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
        title="Which certificate this is",
        description=(
            "This certificate is defined in your topology: its name, the names "
            "it covers and where it installs all come from there. HQ points at "
            "that definition and keeps it issued, deployed and renewed. Leave "
            "it alone unless this resource should track a different "
            "certificate entirely."
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
        title="Domains",
        description=(
            "Wildcards are fine. Each domain has to sit in a Cloudflare zone "
            "HQ can edit — that is how it proves ownership to Let's Encrypt."
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
        title="Renew this many days early",
        description=(
            "HQ renews on its own; this only decides how far ahead of expiry "
            "it starts. It also renews immediately if a consumer is found "
            "serving the wrong certificate."
        ),
    )

    @model_validator(mode="after")
    def one_shape_or_the_other(self):
        # Domains are not part of the choice. What a certificate covers is
        # desired state -- the thing an operator changes when a new domain
        # arrives -- while how a target receives one describes the world and
        # stays in the topology. Counting `domains` as "defining a new
        # certificate" meant a certificate imported from the topology could
        # never have a name added to it in HQ at all.
        defining = bool(self.certificate_name or self.install_on)
        if self.topology_ref and defining:
            raise ValueError(
                "Name an existing certificate or define a new one, not both."
            )
        if not self.topology_ref and not (defining or self.domains):
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
# The order columns are rendered in, and the order a name is wired in: something
# has to run before ingress can reach it, and ingress before a certificate
# secures it.
SERVICE_FACETS: tuple[tuple[str, str], ...] = (
    ("runtime", "Runtime"),
    ("dns", "DNS"),
    ("proxy", "Ingress"),
    ("certificate", "Certificate"),
)
SERVICE_FACET_IDS = frozenset(facet for facet, _ in SERVICE_FACETS)


def service_facets() -> tuple[tuple[str, str], ...]:
    """The facets to render, in catalogue order.

    A facet nothing supplies is a gap in HQ, not in the service, and a column
    with nothing in it tells the operator to go fix something they cannot. So a
    facet may be declared ahead of the provider that fills it and stays
    invisible until that provider is registered.
    """

    supplyable = {provider.facet for provider in PROVIDERS.values() if provider.facet}
    return tuple((facet, label) for facet, label in SERVICE_FACETS if facet in supplyable)


class PortainerContainerSpec(ProviderModel):
    """One container HQ is responsible for keeping up, not for defining.

    Deliberately identity and nothing else. A container's definition lives in
    whatever compose file created it, which HQ has never seen and must not
    pretend to own -- declaring one here says "this is mine to watch and to
    cycle", and reconciliation is locked because there is nothing to converge.

    That is what makes it usable at all. Almost nothing running was created by
    Portainer, so almost nothing can be declared as a stack; every container can
    be started, stopped and restarted, because those are Docker's verbs rather
    than Portainer's.
    """

    connection_ref: str = Field(
        min_length=1,
        max_length=160,
        title="Portainer",
        description="Which Portainer reaches the machine this runs on.",
    )
    host: str = Field(
        min_length=1,
        max_length=160,
        title="Runs on",
        description="The machine this runs on.",
    )
    name: str = Field(
        min_length=1,
        max_length=200,
        title="Container",
        description="The container's name, exactly as Docker reports it.",
    )


class PortainerStackEnvVar(ProviderModel):
    name: str = Field(min_length=1, max_length=200, title="Name")
    value: str = Field(default="", max_length=4000, title="Value")


class PortainerStackSpec(ProviderModel):
    connection_ref: str = Field(
        min_length=1,
        max_length=160,
        title="Portainer",
        description="Which Portainer holds the environment this runs in.",
    )
    host: str = Field(
        min_length=1,
        max_length=160,
        title="Runs on",
        description="The machine this runs on, as the topology names it.",
    )
    name: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        title="Stack name",
        description="Lowercase and hyphenated. Names the compose project.",
    )
    compose: str = Field(
        min_length=1,
        title="Compose file",
        description="The docker compose definition, exactly as it would be on disk.",
    )
    environment: list[PortainerStackEnvVar] = Field(
        default_factory=list,
        title="Environment",
        description="Values the compose file reads. Secrets belong in 1Password, not here.",
    )
    hostnames: list[str] = Field(
        default_factory=list,
        title="Serves",
        description="The names this answers for, if any reach it from outside.",
    )
    port: int | None = Field(
        default=None,
        ge=1,
        le=65535,
        title="Answers on port",
        description=(
            "The port on the machine itself. Published ports are read back from "
            "the running container; a container on the host network has to say."
        ),
    )


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


class UploadedCertificateSpec(ProviderModel):
    """A certificate generated elsewhere, that HQ installs and keeps.

    Separate from ``tls.certificate`` because the lifecycle is different, not
    because the certificate is. This one cannot be renewed by HQ -- the CA that
    signs it is deliberately air-gapped -- so it has no renewal window and no
    automatic renew action, and pretending otherwise would put a countdown on a
    thing HQ cannot act on.
    """

    certificate_name: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[a-z0-9][a-z0-9.-]*$",
        title="Name",
        description="What to call it at the providers it gets installed on.",
    )
    install_on: list[str] = Field(
        min_length=1,
        title="Install it on",
        description="Where to deploy it. It can be added to more later.",
    )


class ResolvedUploadedCertificateSpec(ProviderModel):
    certificate_name: str = Field(min_length=1, max_length=160)
    install_on: list[str] = Field(min_length=1)
    consumers: list[TLSConsumer] = Field(min_length=1)


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


@dataclass(frozen=True)
class DNSRecordType:
    """One record type, and everything the rest of HQ needs to know about it.

    Record types differ in ways that reach every layer: whether the name is
    expected to answer, whether Cloudflare will proxy it, what the value even
    means, and what stops working when it is removed. Stated once here, those
    differences are read by the service view, the form, the reconciler and the
    removal page. Spelled inline instead, each of them grew its own tuple of
    type names and they drifted apart the first time one was extended.
    """

    id: str
    label: str
    # Whether a record of this type brings a service into existence. An address
    # record answers "where does this name point"; every other type states a
    # fact *about* a name without promising that anything serves it. Listing a
    # DMARC policy as a service would put a hostname on the board that nothing
    # is expected to answer, and then report it as unserved forever.
    declares_service: bool
    # What the value is called, and what a correct one looks like. The form is
    # generated from the model, so a type-specific prompt has to come from here
    # rather than from a single field description that is wrong for five types.
    value_label: str
    value_help: str
    # Cloudflare only proxies address records. Offering the toggle elsewhere
    # invites a change the API rejects a minute later, in a job result.
    proxyable: bool = False
    # What breaks if this record goes away. Public DNS is destructive in a way
    # an internal rewrite is not: an internal rewrite that disappears makes one
    # name stop resolving on the LAN, and a missing MX silently bounces mail.
    removal_impact: str = ""
    # Whether this type states policy or proves ownership rather than sending
    # anything anywhere. A zone's day-to-day question is where traffic goes, and
    # a domain apex answers it under four CAA records and three verification
    # strings -- so these are listed apart, folded away, rather than first.
    secondary: bool = False


DNS_RECORD_TYPES: tuple[DNSRecordType, ...] = (
    DNSRecordType(
        "A", "A — IPv4 address", True,
        "IPv4 address", "For example 203.0.113.10.",
        proxyable=True,
        removal_impact="This name stops resolving, so anything served at it goes dark.",
    ),
    DNSRecordType(
        "AAAA", "AAAA — IPv6 address", True,
        "IPv6 address", "For example 2001:db8::10.",
        proxyable=True,
        removal_impact="This name stops resolving over IPv6.",
    ),
    DNSRecordType(
        "CNAME", "CNAME — alias to another name", True,
        "Target hostname", "The name this one is an alias for.",
        proxyable=True,
        removal_impact="This name stops resolving, so anything served at it goes dark.",
    ),
    DNSRecordType(
        "TXT", "TXT — text record", False,
        "Text value",
        "Quoted text. Carries policy such as SPF, or a verification challenge.",
        removal_impact=(
            "If this carries SPF, DMARC or a domain verification, removing it "
            "weakens mail authentication or un-verifies the domain."
        ),
        secondary=True,
    ),
    DNSRecordType(
        "MX", "MX — mail exchanger", False,
        "Mail server hostname", "The host that accepts mail for this domain.",
        removal_impact="Mail for this domain stops being delivered.",
    ),
    DNSRecordType(
        "CAA", "CAA — permitted certificate authority", False,
        "CAA value",
        'Flags, tag and value — for example: 0 issue "letsencrypt.org".',
        removal_impact=(
            "Removing the last CAA record lets any certificate authority in the "
            "world issue for this domain."
        ),
        secondary=True,
    ),
)

DNS_RECORD_TYPES_BY_ID = {record_type.id: record_type for record_type in DNS_RECORD_TYPES}

# Declared statically so the annotation is a real type, and checked against the
# registry below so the two cannot drift.
DNSRecordTypeId = Literal["A", "AAAA", "CNAME", "TXT", "MX", "CAA"]

if set(DNS_RECORD_TYPES_BY_ID) != set(get_args(DNSRecordTypeId)):
    raise ValueError(
        "DNS record type registry and its annotation disagree; a type was "
        "added to one and not the other."
    )

# One expression, used to validate a CAA value and to take it apart. Written
# twice they drifted immediately: the validator accepted a spelling the
# canonicaliser could not parse, so the value passed the form and then never
# matched itself at the provider.
_CAA_VALUE_PARTS = r'^\s*(\d{1,3})\s+(issue|issuewild|iodef)\s+"([^"]*)"\s*$'


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
    record_type: DNSRecordTypeId = Field(title="Record type")
    content: str = Field(
        min_length=1, max_length=2048,
        title="Value",
        description=(
            "What this record says, and it depends on the type chosen above: "
            "an IP address for A and AAAA, a hostname for CNAME and MX, quoted "
            'text for TXT, and for CAA something like 0 issue "letsencrypt.org".'
        ),
    )
    priority: int | None = Field(
        default=None, ge=0, le=65535,
        title="Priority",
        description="MX only. Lower numbers are tried first.",
    )
    proxied: bool = Field(
        default=False,
        title="Proxy through Cloudflare",
        description=(
            "Address records only. On, Cloudflare answers and your address is "
            "never published — caching, WAF and its certificate in front. Off, "
            "the record hands out your address and visitors reach it directly. "
            "Left off by default because it is a real change in who serves the "
            "name, not a default worth assuming."
        ),
    )
    ttl: int = Field(
        default=1, ge=1, le=86400,
        title="TTL",
        description="Seconds resolvers may cache this. 1 means automatic.",
    )

    @model_validator(mode="after")
    def type_shape(self):
        """Reject at the form what Cloudflare would reject a minute later.

        Every rule here is one the API enforces anyway. Enforcing them at the
        edge turns a failed job into a red field next to the answer that caused
        it, which is the difference between a correction and an investigation.
        """

        record_type = DNS_RECORD_TYPES_BY_ID[self.record_type]
        if self.priority is not None and self.record_type != "MX":
            raise ValueError("priority applies only to MX records")
        if self.record_type == "MX" and self.priority is None:
            raise ValueError("an MX record needs a priority")
        if self.proxied and not record_type.proxyable:
            raise ValueError(
                f"Cloudflare cannot proxy a {self.record_type} record"
            )
        if self.proxied and self.ttl != 1:
            # Cloudflare drives the TTL of a proxied record itself and returns 1
            # for it regardless of what was sent. Storing anything else would
            # make every reconciliation report drift against a value the
            # provider will never agree to.
            raise ValueError("a proxied record must leave TTL automatic (1)")
        if self.record_type == "CAA" and not re.match(_CAA_VALUE_PARTS, self.content):
            raise ValueError(
                'a CAA value looks like: 0 issue "letsencrypt.org"'
            )
        return self


class CloudflareZoneSpec(ProviderModel):
    """A domain HQ is responsible for, and the connection that serves it.

    Declaring one is what makes a zone HQ's business. The credential can see
    every zone on the account, which is not the same as HQ having been asked to
    manage them: a parked domain and a live one look identical to a token, and
    only an operator knows which is which.

    It carries no settings yet. Zone posture -- TLS mode, minimum version, HSTS
    -- is the natural next field set here, and is deliberately absent until the
    controller holds a credential that could reconcile it. Declaring desired
    state nothing can act on is how a control plane starts lying.
    """

    zone: str = Field(
        min_length=1, max_length=253,
        title="Domain",
        description="The domain itself, e.g. example.com.",
    )
    connection_ref: str = Field(
        min_length=1, max_length=160,
        title="Served by",
        description="The provider connection that holds this zone.",
    )


@dataclass(frozen=True)
class NameContext:
    """What HQ already knows about a hostname, offered to the next question.

    Every field here was worked out somewhere else on the way in: which zones a
    credential may edit, where something already answers this name, which
    certificate already covers it. Passed rather than re-derived, because a form
    that cannot see them asks for them again -- and a page offering to issue a
    Let's Encrypt certificate for a name in no public zone is not asking, it is
    proposing a failure.

    Declared here beside the providers that read it and built in the application
    layer, which is the half allowed to touch the database. Every field defaults,
    so a caller that knows nothing yet is a legal caller and providers behave as
    they did before any of this existed.
    """

    hostname: str = ""
    # Zones a connected credential can actually edit, as the controller last
    # reported them. Empty means nothing has swept, not that nothing is
    # reachable -- so an empty tuple must never be read as a prohibition.
    public_zones: tuple[str, ...] = ()
    swept: bool = False
    # Where this name is already served, as "host:port", declared or observed.
    # The host half is whatever the provider calls the machine, which for a
    # container stack is the machine's name rather than an address.
    origin: str = ""
    # The same place, as something on the network can actually reach it. A
    # proxy seeded with a machine name is seeded with something nginx cannot
    # resolve, which is a worse answer than an empty box: it looks considered.
    origin_address: str = ""
    # Resource keys of certificates that already cover this name.
    certificates: tuple[str, ...] = ()

    @property
    def public_zone(self) -> str:
        """The reported zone this name falls in, if one does.

        Suffix-matched on label boundaries: "notjseverino.com" is not in
        "jseverino.com", and a check on plain string endings says it is.
        """

        for zone in self.public_zones:
            if self.hostname == zone or self.hostname.endswith(f".{zone}"):
                return zone
        return ""


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
    seed: Callable[["NameContext"], dict[str, Any]] | None = None
    # Some resources are not complete without material the operator has to
    # supply -- an uploaded certificate is only a name and a list of targets
    # until the certificate itself arrives. Declared as a form and a handler so
    # the same page collects both: asked for separately, creating one produced
    # an empty declaration and a second page to go and find.
    material_form: str = ""
    material_handler: str = ""
    # Fields that are routine tuning rather than part of the question being
    # asked. Split on required-ness instead, a spec whose validity comes from a
    # cross-field rule has no required fields at all, and its form rendered
    # empty. Required-ness describes the model; this describes the conversation,
    # and only the provider knows which of its own knobs are which.
    advanced_fields: tuple[str, ...] = ()
    # Groups of fields that are alternatives to one another, as
    # ``((group_id, fields...), ...)``. A spec whose validator says "this or
    # that, never both" rendered as one flat list of every field in both, so a
    # certificate described by the topology showed empty "name it", "domains"
    # and "install on" boxes beside the reference that actually defined it --
    # a page that read as though the certificate covered nothing and was
    # installed nowhere. Declaring the choice lets the form show the branch in
    # use and fold the other away.
    alternatives: tuple[tuple[str, tuple[str, ...]], ...] = ()
    # Fields that say *which* thing this is, and so cannot change once it
    # exists. Offered on an edit form they invite an answer that makes no
    # sense: an existing certificate was asked whether it would rather be "a
    # new certificate defined below", with the fields that would define one
    # folded out of sight. Shown as a fact instead, and dropped from the form.
    fixed_after_create: tuple[str, ...] = ()
    # What changing a field actually causes, as ``((field, sentence), ...)``.
    # Saving a new name onto a certificate is not "saving": HQ notices the
    # deployed certificate no longer covers what is declared and re-issues it
    # within the minute. The page that takes the edit is the only place that
    # can say so beforehand, and a provider is the only thing that knows.
    change_effects: tuple[tuple[str, str], ...] = ()
    # Fields that are optional to the model but unanswerable-by-default when
    # the record does not exist yet. An NPM proxy keeps whatever certificate it
    # already has when this is blank, which is a sensible default for an edit
    # and a guaranteed failure on create: the reconciler refuses to create an
    # HTTPS host with no certificate to bind, a minute later, in a job result.
    required_on_create: tuple[str, ...] = ()
    # What sorts of connection stand behind this, matching what the controller
    # calls them. This is the join between a declaration and the credentials
    # that would carry it out: it tells a form which connections to offer, and
    # the connections page what each one is for.
    #
    # Plural because one resource routinely needs two unrelated credentials. A
    # managed certificate is issued through a DNS token and installed over SSH,
    # and a single field would have had to pick one and be wrong about the
    # other on the page whose whole job is saying what a credential is for.
    connection_providers: tuple[str, ...] = ()
    # Why this provider cannot supply a given name, or "" when it can.
    #
    # An offer that cannot work is worse than no offer: a `.homelab` service was
    # invited to add a Let's Encrypt certificate, which needs a DNS-01 challenge
    # in a zone no credential holds, so the only way to find out was to declare
    # it and read the failure a minute later in a job result.
    #
    # A sentence rather than a boolean, because the page says why -- and the
    # provider is the only thing that knows.
    applies: Callable[["NameContext"], str] | None = None
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
    # ----- Identity ----------------------------------------------------------
    #
    # How to tell that a live record and a declaration are the same thing.
    #
    # This defaults to ``hostnames`` and for two providers that is exactly
    # right: an AdGuard rewrite and an NPM proxy host are each the only record
    # their name can have, so "same name" and "same record" mean the same thing.
    #
    # They stop meaning the same thing the moment a provider holds several
    # records for one name. A zone apex routinely carries three TXT records,
    # four CAA records, two MX records and a CNAME -- nine distinct records, one
    # hostname. Identified by hostname they collapse into one, and adoption
    # picks whichever the provider happened to list first. Worse, the types that
    # carry policy rather than address deliberately declare no hostname at all,
    # so they would report as having no identity and be permanently invisible to
    # the one screen built to find unmanaged things.
    identity: Callable[[dict[str, Any]], tuple[str, ...]] | None = None
    # A readable key to suggest when adopting. Defaults to the hostname and the
    # facet, which is meaningless for a record that has no hostname: every TXT
    # record in a zone would be offered the same empty name.
    key_hint: Callable[[dict[str, Any]], str] | None = None
    # The surface that offers creating one, when it is not the registry's own
    # "what do you want to add?" page. A public DNS record is only meaningful
    # inside a zone: offered from the generic page it has to open by asking
    # which domain, which is the one question the page it belongs on has
    # already answered. Declared here rather than excluded there, so the picker
    # never grows a hand-maintained list of the kinds it is meant to leave out.
    created_from: str = ""
    # What stops working if this particular resource is removed, in a sentence.
    # Read by the confirmation page, which otherwise asks "are you sure" about a
    # row of fields -- and the honest answer to that depends entirely on which
    # row it is. Deleting one of four CAA records is housekeeping; deleting the
    # last MX record stops the domain receiving mail.
    removal_note: Callable[[dict[str, Any]], str] | None = None
    # Whether this declaration describes something HQ made at a provider, or
    # only records a responsibility HQ was given.
    #
    # Removal assumes the first, correctly for almost everything: a rewrite, a
    # proxy host and a DNS record all exist somewhere else, so forgetting the
    # row alone would abandon them. A domain is the exception. HQ did not create
    # the zone and deleting it would be absurd; being responsible for it is the
    # entire content of the declaration, so ceasing to be responsible is the
    # entire content of removing it. Left as the default, there was no way to
    # stop managing a domain at all -- removal was refused because the
    # controller implements no delete, which was true and beside the point.
    declaration_only: bool = False
    # What this declaration holds, as ``(kind, their_field, my_field)``.
    #
    # A domain holds the records published in it, and ceasing to be responsible
    # for the domain has to release them -- left behind, HQ would keep
    # reconciling records in a zone the operator had just said was not its
    # business. Which resources those are is provider knowledge: stated in the
    # use case instead, a generic "forget this declaration" path had the string
    # "cloudflare.dns_record" written into it, and the second provider with
    # anything inside it would have added an ``elif``.
    contains: tuple[str, str, str] | None = None

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
        # Authored names win. Blank means HQ has not been asked to own them yet
        # and the topology's list still stands; saving the form once writes
        # them here, after which adding a domain is an edit rather than a sync.
        "domains": authored.get("domains") or certificate.get("domains", []),
        "consumers": consumers,
        "renewal_window_days": authored["renewal_window_days"],
    }


def _resolve_uploaded(
    authored: dict[str, Any], context: ProviderResolutionContext
) -> dict[str, Any]:
    """Turn chosen install targets into full consumer declarations.

    Same learning as an inline certificate: how a target receives one is a
    property of the target, already written on the dependencies that describe
    how it receives the certificates it has.
    """

    payload = context.topology
    if payload is None:
        raise ValueError("Installing a certificate requires the topology snapshot.")
    profiles = _tls_consumer_profiles(payload)
    consumers = []
    for connection_ref in authored["install_on"]:
        profile = profiles.get(connection_ref)
        if profile is None:
            raise ValueError(
                f"Nothing in the topology describes how {connection_ref!r} "
                "receives a certificate, so HQ cannot install one there."
            )
        if profile["kind"] == "cpanel":
            raise ValueError(
                "A certificate HQ did not issue cannot be installed on shared "
                "hosting: cPanel will not accept one signed by a private CA."
            )
        consumer = {
            "topology_ref": profile["from"],
            "kind": profile["kind"],
            "connection_ref": connection_ref,
            "name": f"{authored['certificate_name']}-{profile['kind']}",
            "verify_domains": [],
        }
        if profile["kind"] == "caddy":
            consumer["certificate_directory"] = profile["certificate_directory"]
        else:
            consumer["discover_covered_hosts"] = False
        consumers.append(consumer)
    return {
        "certificate_name": authored["certificate_name"],
        "install_on": authored["install_on"],
        "consumers": consumers,
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
    # expected to serve, and then permanently report it as unserved. The same is
    # true of MX and CAA, which is why the answer comes from the record-type
    # registry rather than from a list of exceptions maintained here.
    record_type = DNS_RECORD_TYPES_BY_ID.get(spec["record_type"])
    if record_type is None or not record_type.declares_service:
        return ()
    return (spec["name"],)


def _rewrite_readout(
    spec: dict[str, Any], status: dict[str, Any]
) -> tuple[tuple[str, str, str], ...]:
    return (("Answers with", spec.get("answer", ""), status.get("answer", "")),)


def _stack_hostnames(spec: dict[str, Any]) -> tuple[str, ...]:
    return tuple(spec.get("hostnames") or ())


def _stack_origin(spec: dict[str, Any]) -> str:
    """Where this answers, as the topology names the machine.

    ``_locate`` matches a host by id as readily as by address, so a stack says
    which machine it runs on and never repeats that machine's address. A stack
    with no port answers nothing directly -- it is reached through whatever
    fronts it -- and returning nothing is the honest form of that.
    """

    port = spec.get("port")
    host = spec.get("host", "")
    return f"{host}:{port}" if host and port else ""


def _stack_seed(context: NameContext) -> dict[str, Any]:
    """A stack seeded from the name it will serve.

    The name doubles as the stack's own, lowercased and hyphenated the way
    compose projects are, so publishing a service does not ask for it twice.
    """

    label = re.sub(r"[^a-z0-9-]+", "-", context.hostname.lower()).strip("-")
    return {"hostnames": [context.hostname], "name": label or "service"}


def _stack_readout(
    spec: dict[str, Any], status: dict[str, Any]
) -> tuple[tuple[str, str, str], ...]:
    port = spec.get("port")
    where = f"{spec.get('host', '')}:{port}" if port else spec.get("host", "")
    return (
        ("Runs on", where, status.get("origin", "")),
        ("Stack", spec.get("name", ""), status.get("state", "")),
    )


def _stack_from_record(record: dict[str, Any]) -> dict[str, Any]:
    """A declaration matching a container the controller already reported.

    Adopting takes what is running rather than asking for it again: the stack
    name, the machine, and the published port when the container has one. A
    container on the host network publishes nothing, so its port stays for the
    operator to supply -- nothing else knows it.
    """

    return {
        "name": record.get("stack", ""),
        "host": record.get("host", ""),
        "port": record.get("port") or None,
        "compose": record.get("compose", ""),
        "hostnames": list(record.get("hostnames") or ()),
        "connection_ref": record.get("connection_ref", ""),
    }


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


def expiry_phrase(stamp: str) -> str:
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
    consumers = status.get("consumers") or []
    # The names themselves, not how many of them there are. A count answers a
    # question nobody asks: "which names does this cover" is the reason to look
    # at a certificate at all, and "7" is the one reply that cannot be checked.
    installed = sorted(
        {
            str(consumer.get("consumer") or consumer.get("consumer_kind") or "")
            for consumer in consumers
            if isinstance(consumer, dict)
        }
        - {""}
    )
    # Compact on purpose. This readout is what a *service* page shows beside a
    # hostname, and there the question is whether this name is covered by
    # something healthy -- not which seven other names share the certificate.
    # The full list belongs on the certificate, where it is now an editable
    # field rather than a paragraph.
    return (
        ("Issuer", "", status.get("issuer", "")),
        ("Expires", "", expiry_phrase(status.get("not_after", ""))),
        ("Installed on", "", ", ".join(installed)),
    )


def _dns_record_origin(spec: dict[str, Any]) -> str:
    """Where a record sends the name, when the record itself is the answer.

    An internal name is routed by a proxy, so the proxy declares the origin. A
    public name pointed straight at something -- a CNAME to a Pages site, an A
    record to a host -- is routed by the record, and nothing else in HQ was
    saying so: the service page reported "Not routed. Nothing declares where
    requests for this name are served" about a name whose whole configuration
    was a statement of exactly that.

    Only address types answer. A TXT or CAA record routes nothing.
    """

    record_type = DNS_RECORD_TYPES_BY_ID.get(str(spec.get("record_type", "")).upper())
    if record_type is None or not record_type.declares_service:
        return ""
    return str(spec.get("content", "")).strip()


def _dns_record_value(spec: dict[str, Any]) -> str:
    """The record as one line, the way a zone file would state it."""

    parts = [str(spec.get("record_type", "")).strip()]
    if spec.get("priority") is not None:
        parts.append(str(spec["priority"]))
    parts.append(str(spec.get("content", "")).strip())
    return " ".join(part for part in parts if part)


def _dns_record_readout(
    spec: dict[str, Any], status: dict[str, Any]
) -> tuple[tuple[str, str, str], ...]:
    # Both sides through the same formatter. Compared against the bare
    # `content`, the desired value -- which states the type, and a priority
    # when there is one -- could never match what was read back, so every
    # record reported drift against itself while its own health said Healthy.
    observed = _dns_record_value(status) if status.get("content") else ""
    rows = [("Record", _dns_record_value(spec), observed)]
    if spec.get("proxied"):
        # Worth its own row: a proxied record resolves to Cloudflare rather than
        # to the address authored here, so an operator comparing this page
        # against `dig` sees two different answers and needs to know why.
        rows.append(("Proxied", "Through Cloudflare", ""))
    return tuple(rows)


def _dns_record_from_record(record: dict[str, Any]) -> dict[str, Any]:
    """A Cloudflare record, as the spec that would reproduce it exactly.

    Adoption is only safe if the declaration starts out equal to the world, so
    every field the reconciler sends is captured here -- including the ones an
    operator would never think to set. ``priority`` is read back only for MX
    because Cloudflare reports 0 for types that do not have one, and storing
    that would fail the spec's own validation on the next edit.
    """

    spec = {
        "zone": record.get("zone", ""),
        "name": record.get("name", ""),
        "record_type": record.get("record_type", ""),
        "content": record.get("content", ""),
        "proxied": bool(record.get("proxied", False)),
        "ttl": int(record.get("ttl", 1) or 1),
    }
    if record.get("record_type") == "MX":
        spec["priority"] = int(record.get("priority", 0) or 0)
    return spec


def _dns_record_identity(spec: dict[str, Any]) -> tuple[str, ...]:
    """What makes this record itself and not its neighbour.

    Cloudflare's own record id would be the obvious answer and is the wrong one
    here: a declaration authored in HQ has never had one, so identity has to be
    something both a live record and a freshly typed form can produce. The tuple
    a zone file would use -- name, type, value -- is that, and it is unique
    because Cloudflare rejects an exact duplicate of all three.
    """

    name = normalized_hostname(spec.get("name", ""))
    zone = normalized_hostname(spec.get("zone", ""))
    record_type = str(spec.get("record_type", "")).strip().upper()
    content = normalized_record_content(record_type, str(spec.get("content", "")))
    if not (zone and name and record_type and content):
        return ()
    return (zone, name, record_type, content)


def names_a_host(name: str) -> bool:
    """Whether a DNS name could ever be something that answers.

    A label beginning with an underscore is reserved by RFC 8552 for metadata
    about a domain rather than for a host in it: ``_dmarc``, ``_domainkey``,
    ``_acme-challenge``, ``_sip._tcp``. Nothing is ever served there, and no
    name of that shape can be a service however it is published.

    The record type alone could not tell. TXT records were excluded because
    they carry policy, which caught ``_dmarc`` -- and missed
    ``sig1._domainkey``, a DKIM delegation published as a CNAME. The type said
    "an address, so a service"; the name says it is a signing key.
    """

    return not any(label.startswith("_") for label in str(name).split("."))


def normalized_hostname(name: str) -> str:
    """One spelling of a DNS name: lowercase, trimmed, no trailing dot.

    The single implementation. Three modules had their own -- the service view,
    the domain view and this one -- and they agreed only by coincidence. A name
    is the join between every surface HQ has: a rewrite, a proxy host, a
    certificate and a DNS record are authored in four places and will not agree
    on case or on the trailing dot, and two that differ only in those would
    appear as separate services with each missing what the other had.
    """

    return str(name).strip().lower().rstrip(".")


def caa_parts(content: str) -> tuple[int, str, str] | None:
    """A CAA value as its three parts, or None if it is not one.

    Cloudflare returns a CAA record as one formatted string and accepts it only
    as three fields. HQ stores the string, because that is what a zone file shows
    and what an operator recognises, and splits it here -- once, rather than in
    the validator, the canonicaliser and the controller separately, which is
    where the spellings they each accepted began to disagree.
    """

    parsed = re.match(_CAA_VALUE_PARTS, str(content))
    if not parsed:
        return None
    return int(parsed.group(1)), parsed.group(2), parsed.group(3)


def normalized_record_content(record_type: str, content: str) -> str:
    """One spelling of a value, so desired and observed can be compared.

    Declared here, beside the record-type registry, and imported by the
    controller rather than reimplemented there. Two copies of this is not a
    tidiness problem: identity uses it to decide whether a live record is one HQ
    already declares, and the reconciler uses it to decide whether that record
    needs changing. If the two ever disagreed, HQ would adopt a record and then
    immediately rewrite it.

    Every rule is one Cloudflare imposes, and each is a way for a record to
    report as drifted against itself:

    - a TXT value comes back quoted whether or not it was sent that way;
    - a hostname is case-insensitive and comes back lowercased;
    - a CAA value is re-emitted with single spaces.
    """

    value = str(content).strip()
    if record_type == "TXT" and not (value.startswith('"') and value.endswith('"')):
        value = f'"{value}"'
    if record_type in {"CNAME", "MX"}:
        value = value.lower().rstrip(".")
    if record_type == "CAA":
        parts = caa_parts(value)
        if parts:
            flags, tag, target = parts
            value = f'{flags} {tag} "{target}"'
    return value


def _dns_record_key_hint(spec: dict[str, Any]) -> str:
    """A name an operator would recognise on a list of declarations.

    The record type is in it because a name usually has more than one record and
    "jseverino-com" would collide with itself four times over on a zone apex.
    """

    name = normalized_hostname(spec.get("name", ""))
    record_type = str(spec.get("record_type", "")).strip().lower()
    return f"{name}-{record_type}"


def _dns_record_removal_note(spec: dict[str, Any]) -> str:
    record_type = DNS_RECORD_TYPES_BY_ID.get(
        str(spec.get("record_type", "")).upper()
    )
    return record_type.removal_impact if record_type else ""


def _zone_identity(spec: dict[str, Any]) -> tuple[str, ...]:
    zone = normalized_hostname(spec.get("zone", ""))
    return (zone,) if zone else ()


def _zone_key_hint(spec: dict[str, Any]) -> str:
    return normalized_hostname(spec.get("zone", ""))


def _zone_readout(
    spec: dict[str, Any], status: dict[str, Any]
) -> tuple[tuple[str, str, str], ...]:
    """What is true of this domain right now, stated without judgement.

    Every row is an observation. HQ has no credential that could change any of
    them yet, and a control plane that flags drift against a policy it cannot
    enforce is just an opinion with a red pill next to it. When zone posture
    becomes declarable these become desired-vs-observed like every other row.
    """

    return (
        ("Records", "", str(status.get("record_count", "")) if status else ""),
        ("Mail (MX)", "", status.get("mx_summary", "")),
        ("SPF", "", status.get("spf_summary", "")),
        ("DMARC", "", status.get("dmarc_summary", "")),
        ("Certificate authorities (CAA)", "", status.get("caa_summary", "")),
        ("Served by", spec.get("connection_ref", ""), ""),
    )


def _zone_from_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "zone": record.get("zone", ""),
        "connection_ref": record.get("connection_ref", ""),
    }


def _uploaded_certificate_hostnames(spec: dict[str, Any]) -> tuple[str, ...]:
    # The names come from the certificate itself, which HQ read when it was
    # uploaded and recorded on the resource's status. Nothing is declared here,
    # so before the first observation this covers nothing -- which is true.
    return ()


def _uploaded_certificate_readout(
    spec: dict[str, Any], status: dict[str, Any]
) -> tuple[tuple[str, str, str], ...]:
    return (
        ("Name", spec.get("certificate_name", ""), ""),
        ("Expires", "", expiry_phrase(status.get("not_after", ""))),
        ("Installed on", ", ".join(spec.get("install_on", ())), ""),
    )


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


def _rewrite_seed(context: NameContext) -> dict[str, Any]:
    return {"domain": context.hostname}


def _proxy_seed(context: NameContext) -> dict[str, Any]:
    """A proxy for this name, pointed at whatever already serves it.

    The address is not a guess. Something declared or observed answers this name
    on a host and a port, and the form used to open with an empty "Send traffic
    to" beside a page stating exactly that -- so the operator read the answer off
    one card and typed it into the next.

    Blank when nothing serves it yet, which is the honest form of not knowing.
    """

    host, _, port = (context.origin_address or context.origin).rpartition(":")
    seeded: dict[str, Any] = {"domain_names": [context.hostname]}
    if host and port.isdigit():
        seeded["forward_host"] = host
        seeded["forward_port"] = int(port)
    # The certificate that already answers for this name, rather than whichever
    # sorted first. With one certificate the menu was right by luck; the second
    # one would have bound a proxy to a certificate that does not cover it.
    if len(context.certificates) == 1:
        seeded["certificate_resource"] = context.certificates[0]
    return seeded


def _certificate_seed(context: NameContext) -> dict[str, Any]:
    """A new certificate, started from the name that needs one.

    Only the names it covers: what to call its lineage and where to install it
    are decisions nobody can read off a hostname, and guessing either would put
    an answer in the form that looks considered and is not.
    """

    return {"domains": [context.hostname]}


def _dns_record_seed(context: NameContext) -> dict[str, Any]:
    # The registrable domain, guessed from the last two labels. A seed, not a
    # decision: it is offered in an editable field because a zone is not always
    # the last two labels, and being wrong here is visible and one keystroke to
    # correct.
    # The zone a connected credential actually holds, when one does. Falling
    # back to the last two labels, which is right for most names and wrong for
    # every co.uk -- a guess worth making only when there is nothing better.
    labels = context.hostname.split(".")
    zone = context.public_zone or (
        ".".join(labels[-2:]) if len(labels) > 2 else context.hostname
    )
    return {"name": context.hostname, "zone": zone}


def _uploaded_certificate_seed(context: NameContext) -> dict[str, Any]:
    """An uploaded certificate, named after what needs one.

    Seeding the name is the whole of what a hostname can answer here: which
    machines to install it on is a decision, and the certificate itself arrives
    as a file on the same page.

    Its existence is the point. Offered nowhere, a `.homelab` service had one
    certificate option and it was the one that cannot work -- the answer was
    reachable only by knowing to go to the registry and pick it by hand.
    """

    label = re.sub(r"[^a-z0-9.-]+", "-", context.hostname.lower()).strip("-.")
    return {"certificate_name": label or "certificate"}


def _container_readout(
    spec: dict[str, Any], status: dict[str, Any]
) -> tuple[tuple[str, str, str], ...]:
    return (
        ("Container", spec.get("name", ""), status.get("container", "")),
        ("Runs on", spec.get("host", ""), status.get("host", "")),
        ("State", "", status.get("state", "")),
    )


def _container_from_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "connection_ref": record.get("connection_ref", ""),
        "host": record.get("host", ""),
        "name": record.get("name", ""),
    }


def _container_identity(spec: dict[str, Any]) -> tuple[str, ...]:
    return (spec.get("host", ""), spec.get("name", ""))


def _container_key_hint(spec: dict[str, Any]) -> str:
    host = spec.get("host", "")
    name = spec.get("name", "")
    return re.sub(r"[^a-z0-9-]+", "-", f"{host}-{name}".lower()).strip("-")


def _container_removal_note(spec: dict[str, Any]) -> str:
    return (
        f"HQ stops watching {spec.get('name', 'this container')} and can no "
        "longer start, stop or restart it. The container itself is untouched."
    )


def _public_dns_applies(context: NameContext) -> str:
    """Whether any connected account holds a zone this name could live in.

    A `.homelab` name has no public zone and never will, so offering to publish
    a record for it proposes a call Cloudflare will refuse. The credential
    already reported which zones it may edit; this is that answer, used.

    Silent until something has swept. An empty report means nobody has looked,
    and refusing every name on that basis would make one missed sweep look like
    a deliberate restriction.
    """

    if not context.swept or context.public_zone:
        return ""
    return "No connected DNS account holds a zone for this name."


def _managed_certificate_applies(context: NameContext) -> str:
    """Whether Let's Encrypt could issue for this name at all.

    Issuance here is DNS-01, which means proving control by writing a record
    into the name's own zone. No zone, no proof, and no certificate -- a fact
    knowable now rather than a minute later in a failed job.
    """

    if not context.swept or context.public_zone:
        return ""
    return (
        "Let's Encrypt proves this name by writing a DNS record in its zone, "
        "and no connected account holds one. Upload a certificate instead."
    )


_PROVIDERS = (
    ProviderSpec(
        "tls.certificate",
        "Issues a certificate from Let's Encrypt and keeps it renewed and "
        "installed on everything that serves these names. Nothing to upload.",
        TLSCertificateSpec,
        ResolvedTLSCertificateSpec,
        _resolve_tls,
        label="TLS certificate",
        applies=_managed_certificate_applies,
        connection_providers=("cloudflare_dns", "ssh"),
        choices="application.provider_choices:certificate_choices",
        # topology_ref is not offered when adding one. It exists because every
        # certificate here predates HQ owning them, and "add the certificate you
        # already have" is not a thing anyone wants to do -- it is already
        # managed. Adding means defining a new one; the reference stays visible
        # on the resources that use it.
        advanced_fields=("topology_ref", "renewal_window_days"),
        alternatives=(
            ("described", ("topology_ref",)),
            ("defined", ("certificate_name", "install_on")),
        ),
        fixed_after_create=("topology_ref",),
        change_effects=(
            (
                "domains",
                "Saving re-issues the certificate and redeploys it everywhere "
                "it is installed. Takes about a minute.",
            ),
        ),
        facet="certificate",
        readout=_certificate_readout,
        hostnames=_certificate_hostnames,
        seed=_certificate_seed,
        covers=True,
    ),
    ProviderSpec(
        "tls.uploaded_certificate",
        "Installs a certificate you generated yourself, and keeps it so you can "
        "add it to another service without regenerating it.",
        UploadedCertificateSpec,
        ResolvedUploadedCertificateSpec,
        _resolve_uploaded,
        label="Uploaded certificate",
        seed=_uploaded_certificate_seed,
        connection_providers=("ssh",),
        choices="application.provider_choices:uploaded_certificate_choices",
        material_form="application.provider_forms:CertificateUploadForm",
        material_handler="application.certificates:store_uploaded_material",
        facet="certificate",
        hostnames=_uploaded_certificate_hostnames,
        covers=True,
        readout=_uploaded_certificate_readout,
    ),
    ProviderSpec(
        "npm.proxy_host",
        "Sends a hostname to something running on your network, over HTTPS. "
        "Created in Nginx Proxy Manager if it is not there yet.",
        NPMProxyHostSpec,
        ResolvedNPMProxyHostSpec,
        _resolve_npm,
        label="Proxy host",
        connection_providers=("npm",),
        removal_note=lambda spec: (
            "Every name this answers for stops being served: "
            + ", ".join(spec.get("domain_names", ()))
            + "."
        ),
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
        "portainer.stack",
        "Runs a set of containers on one of your machines. Created in Portainer "
        "if it is not there yet.",
        PortainerStackSpec,
        label="Container stack",
        connection_providers=("portainer",),
        # Which Portainer is folded away with the tuning. There is normally one,
        # the menu selects it, and asking first makes the form open on the
        # question an operator is least likely to have an opinion about.
        advanced_fields=("connection_ref", "environment"),
        removal_note=lambda spec: (
            f"{spec.get('name', 'This stack')} stops running on "
            f"{spec.get('host', 'its machine')}, and anything it serves goes "
            "with it."
        ),
        facet="runtime",
        hostnames=_stack_hostnames,
        origin=_stack_origin,
        seed=_stack_seed,
        readout=_stack_readout,
        from_record=_stack_from_record,
        choices="application.provider_choices:container_stack",
    ),
    ProviderSpec(
        "portainer.container",
        "A container HQ keeps an eye on and can start, stop or restart. It "
        "does not define the container -- whatever compose file created it "
        "still does.",
        PortainerContainerSpec,
        label="Container",
        connection_providers=("portainer",),
        # No facet, no hostnames and no seed, so it is never offered as a way
        # to publish a name. A container answers wherever its ports are pointed
        # and the declaration does not say where that is; inventing a hostname
        # from a container name would put a service on the board that no name
        # reaches. It is adopted from what a sweep found, which is the only
        # place its identity is known.
        readout=_container_readout,
        from_record=_container_from_record,
        identity=_container_identity,
        key_hint=_container_key_hint,
        removal_note=_container_removal_note,
        declaration_only=True,
        choices="application.provider_choices:container_stack",
    ),
    ProviderSpec(
        "adguard.rewrite",
        "Makes a hostname resolve to an IP on your network. Created in AdGuard "
        "if it is not there yet.",
        AdGuardRewriteSpec,
        label="Internal DNS record",
        connection_providers=("adguard",),
        removal_note=lambda spec: (
            f"{spec.get('domain', 'This name')} stops resolving on the LAN, so "
            "anything reached by that name goes dark inside the network."
        ),
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
        applies=_public_dns_applies,
        connection_providers=("cloudflare_dns",),
        advanced_fields=("priority", "ttl"),
        public_effect=True,
        facet="dns",
        hostnames=_dns_record_hostnames,
        seed=_dns_record_seed,
        readout=_dns_record_readout,
        from_record=_dns_record_from_record,
        choices="application.provider_choices:dns_record",
        identity=_dns_record_identity,
        key_hint=_dns_record_key_hint,
        origin=_dns_record_origin,
        created_from="zone",
        removal_note=_dns_record_removal_note,
    ),
    ProviderSpec(
        "cloudflare.zone",
        "A domain HQ is responsible for. Declaring one is what puts its "
        "records under HQ's management; the credential can see every zone on "
        "the account, which is not the same as being asked to manage them.",
        CloudflareZoneSpec,
        label="Domain",
        connection_providers=("cloudflare_dns",),
        public_effect=True,
        hostnames=None,
        readout=_zone_readout,
        from_record=_zone_from_record,
        choices="application.provider_choices:zone",
        identity=_zone_identity,
        key_hint=_zone_key_hint,
        declaration_only=True,
        contains=("cloudflare.dns_record", "zone", "zone"),
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


def controller_id() -> str:
    """Which controller this deployment runs.

    An identity, not a policy: it names one installation, so it arrives from the
    environment rather than from the committed contract beside it.

    Falling back to the machine's own name rather than to a word. This is what
    a sweep files its findings under, so a placeholder would put every container
    on a host called "controller" -- and both processes that ask run on the host
    network, so both get the same answer without anything being passed between
    them.
    """

    return os.environ.get("HQ_CONTROLLER_ID", "").strip() or os.uname().nodename


def controller_capabilities() -> dict[str, Any]:
    """Return the one validated, JSON-safe controller contract."""

    contract = controller_capability_registry().model_dump(mode="json")
    contract["controller_id"] = controller_id()
    return contract


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
                # Part of the contract, not a detail of one page: anything that
                # offers "add a resource" has to know which kinds stand on their
                # own and which only make sense inside something else.
                "created_from": provider.created_from,
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
