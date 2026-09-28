"""Typed provider declarations: emit once, derive every adapter contract."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType
from typing import Annotated, Any, Callable, Literal, get_args

from django.urls import NoReverseMatch, reverse
from django.urls.converters import StringConverter

from pydantic import (
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from application.github_public import GitHubRepositoryURL

from .attribution import unattributed_kinds
from .consoles import cloudflare_dashboard, tailscale_machine
from .names import certificate_covers, normalized_hostname
from .connection_kinds import CONNECTION_CREDENTIALS
from .observations import OBSERVATIONS
from .provider_adapters import CONTROLLER_PROVIDER_ADAPTERS
from .provider_adapters.contracts import admit_controller_adapters
from .provider_spec import (
    SERVICE_FACETS,
    ControllerCapabilityRegistry,
    ControllerProviderCapability,
    ControllerVerification,
    NameContext,
    ProviderModel,
    ProviderResolutionContext,
    ProviderSpec,
    applies,
    expiry_phrase,
    locked,
)


class TLSConsumerBase(ProviderModel):
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
    # Names whose cPanel *sites* receive the certificate. Empty means every site
    # that serves one of `verify_domains`; the controller asks the account which
    # sites those are before anything is issued.
    install_domains: list[str] = Field(default_factory=list)


TLSConsumer = Annotated[
    CaddyTLSConsumer | NPMTLSConsumer | CPanelTLSConsumer,
    Field(discriminator="kind"),
]


class OnePasswordPublication(ProviderModel):
    """Where one certificate's facts get written, and nothing about what.

    The item and the vault are the whole of it. Which facts are published is
    decided by the adapter that publishes them and cannot be stated here: a
    declaration that could name a field could name a field worth stealing, and
    this is the one target reached with a credential that can write.
    """

    kind: Literal["onepassword"]
    name: str = Field(min_length=1, max_length=160)
    connection_ref: str = Field(min_length=1, max_length=160)
    vault: str = Field(min_length=1, max_length=160)
    item: str = Field(min_length=1, max_length=160)


class TLSDeliveryTargetSpec(ProviderModel):
    """One place a certificate can be installed, and how it arrives there.

    A Caddy host wants its certificate in a particular directory; a cPanel
    account takes only the names it actually hosts. Those are properties of the
    target, true of every certificate it will ever serve, so they are stated
    once here instead of on each certificate that installs there.

    Flat rather than a union per kind, because the form an operator fills is
    generated from this model's fields: a union has none, and the four shapes
    differ by one or two fields each.

    One kind receives no certificate at all. A password manager is where an
    operator already looks up what a credential is and when it runs out, and a
    certificate is both of those, so it is a place a certificate can be
    *recorded*, reached the same way, declared the same way, and listed in the
    same menu. It carries no material; see ``OnePasswordPublication``.
    """

    kind: Literal["npm", "caddy", "cpanel", "onepassword"] = Field(
        title="Type",
        description="Sets how the certificate is delivered and verified.",
    )
    connection_ref: str = Field(
        min_length=1,
        max_length=160,
        title="Connection",
        description="The connection HQ uses to reach this target.",
    )
    name: str = Field(
        min_length=1,
        max_length=160,
        title="Name at target",
        description=(
            "The certificate's name on the target. Only the certificate below "
            "uses it. Others installed here keep their own names."
        ),
    )
    certificate_resource: str = Field(
        default="",
        max_length=160,
        title="Certificate using this name",
        description=(
            "The certificate that uses the name above. Blank if none."
        ),
    )
    verify_domains: list[str] = Field(
        default_factory=list,
        title="Check these names",
        description=(
            "Names HQ connects to here to confirm the certificate arrived. "
            "Leave empty to check every name it covers."
        ),
    )
    certificate_directory: str = Field(
        default="",
        max_length=500,
        title="Certificate directory",
        description=(
            "Caddy only. The directory the certificate and key are written to."
        ),
    )
    discover_covered_hosts: bool = Field(
        default=False,
        title="Check every proxy host it covers",
        description=(
            "Nginx Proxy Manager only. Check every proxy host whose name this "
            "certificate covers, in addition to the names above."
        ),
    )
    install_domains: list[str] = Field(
        default_factory=list,
        title="Install only on the sites serving these names",
        description=(
            "cPanel only. cPanel holds one certificate per site, shared by the "
            "site's aliases. Leave empty to install on every site serving a "
            "name this certificate is checked at."
        ),
    )
    vault: str = Field(
        default="",
        max_length=160,
        title="Vault",
        description="1Password only. The vault that holds the item.",
    )
    item: str = Field(
        default="",
        max_length=160,
        title="Item",
        description=(
            "1Password only. The item HQ writes the certificate's details to. "
            "It must already exist. HQ does not create it."
        ),
    )

    @model_validator(mode="after")
    def kind_decides_which_settings_apply(self):
        # Refused rather than ignored. A directory typed against an NPM target
        # would sit there looking configured while nothing ever read it.
        for field_name, kind in (
            ("certificate_directory", "caddy"),
            ("discover_covered_hosts", "npm"),
            ("install_domains", "cpanel"),
            ("vault", "onepassword"),
            ("item", "onepassword"),
        ):
            if getattr(self, field_name) and self.kind != kind:
                raise ValueError(
                    f"{TLSDeliveryTargetSpec.model_fields[field_name].title!r} "
                    f"applies only to {kind} targets. This one is {self.kind}."
                )
        if self.kind == "caddy" and not self.certificate_directory:
            raise ValueError("A Caddy target needs a certificate directory.")
        if self.kind == "onepassword" and not (self.vault and self.item):
            raise ValueError(
                "A 1Password target needs a vault and an item."
            )
        # Nothing is served here, so there is nothing to connect to and check.
        # Refused rather than ignored, for the same reason as the rest: a name
        # typed here would read as verified and never be probed.
        if self.kind == "onepassword" and self.verify_domains:
            raise ValueError(
                "A 1Password target serves nothing. Leave the names to check "
                "empty."
            )
        return self


class TLSCertificateSpec(ProviderModel):
    """One certificate HQ issues, deploys and keeps renewed.

    Everything about it is stated here: what it is called, which names it
    covers, and where it installs. Adding a name is saving the form.

    Titles and descriptions live on the model because the form is generated
    from it.
    """

    certificate_name: str = Field(
        max_length=160,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        title="Certificate name",
        description="Lowercase, no spaces.",
    )
    domains: list[str] = Field(
        default_factory=list,
        title="Domains",
        description=(
            "Wildcards are allowed. Each domain must be in a Cloudflare zone "
            "HQ can edit, for the Let's Encrypt DNS challenge."
        ),
    )
    install_on: list[str] = Field(
        default_factory=list,
        title="Install it on",
        description=(
            "Where the certificate is deployed. Delivery settings are on each "
            "target."
        ),
    )

    renewal_window_days: int = Field(
        default=30,
        ge=1,
        le=60,
        title="Renew this many days early",
        description=(
            "Days before expiry that renewal starts. HQ also renews at once "
            "if a consumer serves the wrong certificate."
        ),
    )

    @model_validator(mode="after")
    def a_certificate_needs_names_and_somewhere_to_go(self):
        missing = [
            label
            for label, value in (
                ("the names it covers", self.domains),
                ("somewhere to install it", self.install_on),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"{self.certificate_name} still needs " + " and ".join(missing) + "."
            )
        return self


class ResolvedTLSCertificateSpec(ProviderModel):
    certificate_name: str = Field(min_length=1, max_length=160)
    domains: list[str] = Field(min_length=1)
    consumers: list[TLSConsumer] = Field(min_length=1)
    # Separate from the consumers, because a consumer is something that serves
    # the certificate and is checked by being connected to. These are places the
    # certificate is written *about*. Folded into the same list they would be
    # probed for a TLS handshake against a password manager, and a target that
    # cannot answer one would read as a target that failed to.
    publish_to: list[OnePasswordPublication] = Field(default_factory=list)
    renewal_window_days: int = Field(default=30, ge=1, le=60)

    @field_validator("domains")
    @classmethod
    def normalize_domains(cls, domains: list[str]) -> list[str]:
        normalized = [normalized_hostname(domain) for domain in domains]
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



def service_facets() -> tuple[tuple[str, str], ...]:
    """The facets to render, in catalogue order.

    A facet nothing supplies is a gap in HQ, not in the service, and a column
    with nothing in it tells the operator to go fix something they cannot. So a
    facet may be declared ahead of the provider that fills it and stays
    invisible until that provider is registered.
    """

    supplyable = {provider.facet for provider in PROVIDERS.values() if provider.facet}
    return tuple(
        (facet, label) for facet, label in SERVICE_FACETS if facet in supplyable
    )


class PortainerContainerSpec(ProviderModel):
    """One container HQ is responsible for keeping up, not for defining.

    Deliberately identity and nothing else. A container's definition lives in
    whatever compose file created it, which HQ has never seen and must not
    pretend to own: declaring one here says "this is mine to watch and to
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
        description="The Portainer that manages this machine.",
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
        description="The container name, as Docker reports it.",
    )
    on_demand: bool = Field(
        default=False,
        title="Runs on demand",
        description=(
            "Usually stopped and removed. A sweep that misses it is not a "
            "finding."
        ),
    )
    holds_docker_socket: bool = Field(default=False, title="Holds the Docker socket", description="Its job is the socket: a socket proxy or an agent. Listed, never an action item.")
    hidden: bool = Field(
        default=False,
        title="Collapse on machine page",
        description=(
            "Collapse it on the machine's page. HQ still watches and controls "
            "it."
        ),
    )
    serves_ports: list[int] = Field(
        default_factory=list,
        title="Answers on",
        description=(
            "Host-network containers only. Docker reports no ports for them, "
            "so list the ports here to link a proxy to it."
        ),
    )
    source: GitHubRepositoryURL = Field(
        default="",
        max_length=300,
        title="Built from",
        description=(
            "The GitHub repository its image is built from, for an image that "
            "does not say. HQ reads its releases and advisories."
        ),
    )

    @field_validator("serves_ports")
    @classmethod
    def ports_are_ports(cls, value: list[int]) -> list[int]:
        if any(port < 1 or port > 65535 for port in value):
            raise ValueError("Ports must be between 1 and 65535.")
        return value


class TailnetDeviceSpec(ProviderModel):
    """A machine on the tailnet whose settings HQ keeps, not one it created.

    The same shape as a watched container: the device joined the tailnet by
    somebody running `tailscale up` on it, and HQ has no business pretending
    otherwise. What it can hold is the handful of decisions about that device
    which are made once and then quietly forgotten, and which have no symptom
    until the day they matter.

    Named as the tailnet names it. That is often not what HQ calls the machine,
    and the join between the two is the address they share; using HQ's name here
    would mean the controller had to guess which device was meant.
    """

    # Optional, like the policy's: a device is adopted from a reading the
    # daemon gave for free, which names no credential, and the reconciler
    # resolves the single Tailscale connection when this is blank. Required, it
    # made every device fail adoption on a field the record could never carry.
    connection_ref: str = Field(
        default="",
        max_length=160,
        title="Tailscale",
        description="The connection HQ uses to change this device.",
    )
    name: str = Field(
        min_length=1,
        max_length=200,
        title="Device",
        description="The device name, as the tailnet reports it.",
    )
    key_expiry_disabled: bool = Field(
        default=False,
        title="Disable key expiry",
        description=(
            "Disables node key expiry. Without it, the device becomes "
            "unreachable when its key expires."
        ),
    )


class TailnetPolicySpec(ProviderModel):
    """The tailnet's access policy, as HQ last read it.

    Not something an operator adds here: it exists because a tailnet does.
    ``created_from`` keeps it out of the "add a resource" picker for that
    reason: there is exactly one, and it arrived with the credential.
    """

    connection_ref: str = Field(default="", max_length=160, title="Tailscale")
    document: str = Field(
        default="",
        title="Policy",
        description=(
            "The tailnet's access policy. Saving records it. Reconciling "
            "applies it if the policy's own tests pass."
        ),
    )


class NetworkSpec(ProviderModel):
    """A range of addresses this estate is built on, and what it means.

    Declared, because nothing sweeps a network. HQ learns addresses from the
    things that answer at them (a container's published port, a device's
    tailnet address) and never learns what range they belong to or what that
    range implies. An address on the LAN and one on the tailnet are reachable
    by different people, and only the ranges say which is which.
    """

    name: str = Field(
        min_length=1,
        max_length=120,
        title="Name",
        description="A short name for this range.",
    )
    cidr: str = Field(
        min_length=1,
        max_length=64,
        title="Range",
        description="The range in CIDR form, e.g. 198.51.100.0/24.",
    )
    gateway: str = Field(
        default="",
        max_length=64,
        title="Gateway",
        description="The range's router, if it has one.",
    )
    purpose: str = Field(
        default="",
        max_length=300,
        title="Purpose",
        description=(
            "One line. What uses this range."
        ),
    )

    @field_validator("cidr")
    @classmethod
    def a_real_range(cls, value: str) -> str:
        import ipaddress

        try:
            ipaddress.ip_network(value, strict=False)
        except ValueError as exc:
            raise ValueError("Not a valid CIDR range.") from exc
        return value


class CertificateAuthoritySpec(ProviderModel):
    """A certificate authority this estate trusts, and where its key lives.

    Not `tls.certificate`: that is a certificate HQ renews and installs. This is
    the authority a certificate was issued *by*, including one HQ can never
    reach, because the whole point of an offline root is that nothing can. An
    authority nothing sweeps still has an expiry, and an expiry nobody is
    watching is the failure this records.
    """

    name: str = Field(
        min_length=1,
        max_length=160,
        title="Authority",
        description="The issuer name, as it appears in certificates.",
    )
    covers: str = Field(
        default="",
        max_length=300,
        title="Issues",
        description="One line. The certificates this authority signs.",
    )
    expires_on: str = Field(
        default="",
        max_length=10,
        title="Expires",
        description="ISO date, e.g. 2036-05-02. Blank if it does not expire.",
    )
    key_location: str = Field(
        default="",
        max_length=300,
        title="Key location",
        description=(
            "Where the private key is stored. Never paste the key."
        ),
    )
    issued_with: str = Field(
        default="",
        max_length=160,
        title="Issued with",
        description="The signing tool, if any.",
    )

    @field_validator("expires_on")
    @classmethod
    def a_real_date(cls, value: str) -> str:
        if not value:
            return value
        from datetime import date

        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Use an ISO date, e.g. 2036-05-02.") from exc
        return value


class MachineSpec(ProviderModel):
    """A machine HQ should know about, whether or not it can reach one.

    Most machines need no declaration: a swept Portainer names the ones it
    manages, and a connection names what it points at. This is for the rest
    (the printer, the offline CA, the phone) and for saying what an address
    belongs to, which is the difference between a proxy forwarding to a machine
    and a proxy forwarding into the dark.
    """

    # The machine page's `<str:name>` converter: a name it cannot route to
    # would be a machine with no page.
    name: str = Field(
        min_length=1,
        max_length=160,
        pattern=rf"^{StringConverter.regex}$",
        title="Name",
        description="The machine's name in HQ. No slashes.",
    )
    role: str = Field(
        default="",
        max_length=200,
        title="Purpose",
        description="One line. Shown wherever the machine is listed.",
    )
    addresses: list[str] = Field(
        default_factory=list,
        title="Addresses",
        description=(
            "Every address it answers on: LAN, tailnet, public. Resources "
            "forwarding to these addresses link to this machine. Marked "
            "addresses are also observed by HQ."
        ),
    )

    # No operating system field: the tailnet reports `os` for every device.


class PortainerStackEnvVar(ProviderModel):
    name: str = Field(min_length=1, max_length=200, title="Name")
    value: str = Field(default="", max_length=4000, title="Value")


class PortainerStackSpec(ProviderModel):
    connection_ref: str = Field(
        min_length=1,
        max_length=160,
        title="Portainer",
        description="The Portainer environment this runs in.",
    )
    host: str = Field(
        min_length=1,
        max_length=160,
        title="Runs on",
        description="The machine this runs on.",
    )
    name: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        title="Stack name",
        description="Lowercase and hyphenated. Used as the compose project name.",
    )
    compose: str = Field(
        min_length=1,
        title="Compose file",
        description="The docker compose file, as it would be on disk.",
    )
    environment: list[PortainerStackEnvVar] = Field(
        default_factory=list,
        title="Environment",
        description="Values the compose file reads. Keep secrets in 1Password.",
    )
    hostnames: list[str] = Field(
        default_factory=list,
        title="Serves",
        description="Hostnames that reach this stack from outside, if any.",
    )
    port: int | None = Field(
        default=None,
        ge=1,
        le=65535,
        title="Answers on port",
        description=(
            "The port on the machine. Needed only for host-network containers. "
            "Published ports are read from the running container."
        ),
    )


class UploadedCertificateSpec(ProviderModel):
    """A certificate generated elsewhere, that HQ installs and keeps.

    Separate from ``tls.certificate`` because the lifecycle is different, not
    because the certificate is. This one cannot be renewed by HQ (the CA that
    signs it is deliberately air-gapped) so it has no renewal window and no
    automatic renew action, and pretending otherwise would put a countdown on a
    thing HQ cannot act on.
    """

    certificate_name: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[a-z0-9][a-z0-9.-]*$",
        title="Name",
        description="The certificate's name where it is installed.",
    )
    install_on: list[str] = Field(
        min_length=1,
        title="Install it on",
        description="Where to deploy it. You can add more later.",
    )
    domains: list[str] = Field(
        default_factory=list,
        title="Names it covers",
        description=(
            "Read from the certificate on each upload. Remove names HQ should "
            "not treat as covered."
        ),
    )


class ResolvedUploadedCertificateSpec(ProviderModel):
    certificate_name: str = Field(min_length=1, max_length=160)
    install_on: list[str] = Field(min_length=1)
    consumers: list[TLSConsumer] = Field(min_length=1)
    domains: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class DNSRecordType:
    """One record type, and everything the rest of HQ needs to know about it.

    Record types differ in ways that reach every layer: whether the name is
    expected to answer, whether Cloudflare will proxy it, what the value even
    means, and what stops working when it is removed. Stated once here, those
    differences are read by the service view, the form, the reconciler and the
    removal page.
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
    # strings, so these are listed apart, folded away, rather than first.
    secondary: bool = False


DNS_RECORD_TYPES: tuple[DNSRecordType, ...] = (
    DNSRecordType(
        "A",
        "A: IPv4 address",
        True,
        "IPv4 address",
        "For example 203.0.113.10.",
        proxyable=True,
        removal_impact="This name stops resolving. Anything served at it goes offline.",
    ),
    DNSRecordType(
        "AAAA",
        "AAAA: IPv6 address",
        True,
        "IPv6 address",
        "For example 2001:db8::10.",
        proxyable=True,
        removal_impact="This name stops resolving over IPv6.",
    ),
    DNSRecordType(
        "CNAME",
        "CNAME: alias to another name",
        True,
        "Target hostname",
        "The name this one is an alias for.",
        proxyable=True,
        removal_impact="This name stops resolving. Anything served at it goes offline.",
    ),
    DNSRecordType(
        "TXT",
        "TXT: text record",
        False,
        "Text value",
        "Quoted text, such as SPF policy or a verification challenge.",
        removal_impact=(
            "If this is SPF, DMARC or a domain verification, mail "
            "authentication weakens or the domain loses verification."
        ),
        secondary=True,
    ),
    DNSRecordType(
        "MX",
        "MX: mail exchanger",
        False,
        "Mail server hostname",
        "The host that accepts mail for this domain.",
        removal_impact="Mail for this domain stops being delivered.",
    ),
    DNSRecordType(
        "CAA",
        "CAA: permitted certificate authority",
        False,
        "CAA value",
        'Flags, tag and value, e.g. 0 issue "letsencrypt.org".',
        removal_impact=(
            "If this is the last CAA record, any certificate authority can "
            "issue for this domain."
        ),
        secondary=True,
    ),
)

DNS_RECORD_TYPES_BY_ID = {
    record_type.id: record_type for record_type in DNS_RECORD_TYPES
}

# Declared statically so the annotation is a real type, and checked against the
# registry below so the two cannot drift.
DNSRecordTypeId = Literal["A", "AAAA", "CNAME", "TXT", "MX", "CAA"]

if set(DNS_RECORD_TYPES_BY_ID) != set(get_args(DNSRecordTypeId)):
    raise ValueError(
        "DNS record type registry and its annotation disagree; a type was "
        "added to one and not the other."
    )

# One expression, used both to validate a CAA value and to take it apart, so a
# value the form accepts is one the canonicaliser can parse.
_CAA_VALUE_PARTS = r'^\s*(\d{1,3})\s+(issue|issuewild|iodef)\s+"([^"]*)"\s*$'


class CloudflareDNSRecordSpec(ProviderModel):
    zone: str = Field(
        min_length=1,
        max_length=253,
        title="Zone",
        description="The record's domain, e.g. example.com.",
    )
    name: str = Field(
        min_length=1,
        max_length=253,
        title="Hostname",
        description="The full name, e.g. app.example.com.",
    )
    record_type: DNSRecordTypeId = Field(title="Record type")
    content: str = Field(
        min_length=1,
        max_length=2048,
        title="Value",
        description=(
            "An IP address for A and AAAA, a hostname for CNAME and MX, quoted "
            'text for TXT, or for CAA, e.g. 0 issue "letsencrypt.org".'
        ),
    )
    priority: int | None = Field(
        default=None,
        ge=0,
        le=65535,
        title="Priority",
        description="MX only. Lower numbers are tried first.",
    )
    proxied: bool = Field(
        default=False,
        title="Proxy through Cloudflare",
        description=(
            "A, AAAA and CNAME only. On: Cloudflare answers, hides your address "
            "and adds caching, WAF and its certificate. Off: visitors reach "
            "your address directly."
        ),
    )
    ttl: int = Field(
        default=1,
        ge=1,
        le=86400,
        title="TTL",
        description="Seconds resolvers may cache this. 1 is automatic.",
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
            raise ValueError(f"Cloudflare cannot proxy a {self.record_type} record")
        if self.proxied and self.ttl != 1:
            # Cloudflare drives the TTL of a proxied record itself and returns 1
            # for it regardless of what was sent. Storing anything else would
            # make every reconciliation report drift against a value the
            # provider will never agree to.
            raise ValueError("a proxied record must leave TTL automatic (1)")
        if self.record_type == "CAA" and not re.match(_CAA_VALUE_PARTS, self.content):
            raise ValueError('a CAA value looks like: 0 issue "letsencrypt.org"')
        return self


class CloudflareZoneSpec(ProviderModel):
    """A domain HQ is responsible for, and the connection that serves it.

    Declaring one is what makes a zone HQ's business. The credential can see
    every zone on the account, which is not the same as HQ having been asked to
    manage them: a parked domain and a live one look identical to a token, and
    only an operator knows which is which.

    It carries no settings yet. Zone posture (TLS mode, minimum version, HSTS)
    is read through `cloudflare_api`; declaring and reconciling it is the next
    field set here.
    """

    zone: str = Field(
        min_length=1,
        max_length=253,
        title="Domain",
        description="The domain itself, e.g. example.com.",
    )
    connection_ref: str = Field(
        min_length=1,
        max_length=160,
        title="Served by",
        description="The connection that holds this zone.",
    )


def _delivery_target(
    connection_ref: str, context: ProviderResolutionContext
) -> dict[str, Any]:
    for target in context.delivery_targets:
        if target.get("connection_ref") == connection_ref:
            return target
    raise ValueError(
        f"{connection_ref!r} is not a certificate target. Add it as one "
        "first."
    )


def _consumer_at(
    target: dict[str, Any],
    *,
    certificate_key: str,
    certificate_name: str,
    domains: list[str],
    names_at: Callable[[str], tuple[str, ...]] | None = None,
) -> dict[str, Any]:
    """One certificate's declaration of how it arrives at one target.

    The name is the target's own only for the certificate that owns it. Any
    other certificate installed there is named after itself, or the second
    would land on top of the first.

    Which names to check at that target is derived, not typed: a name is a
    consumer at this target when the certificate covers it and it is observed
    landing on that target's machine. The declared list is unioned in, because a
    target may serve a name no sweep can see.

    Derivation only proposes. Each name is still probed at the target and
    matched on fingerprint by the controller, so a name derived wrongly shows up
    as a mismatch rather than as a false claim of coverage.
    """

    kind = target["kind"]
    owns_the_name = bool(certificate_key) and (
        target.get("certificate_resource") == certificate_key
    )
    covered = set(domains)
    landing = names_at(target["connection_ref"]) if names_at else ()
    consumer = {
        "kind": kind,
        "connection_ref": target["connection_ref"],
        "name": target["name"] if owns_the_name else f"{certificate_name}-{kind}",
        "verify_domains": sorted(
            {
                *(target.get("verify_domains") or []),
                *(name for name in landing if certificate_covers(name, covered)),
            }
        ),
    }
    if kind == "caddy":
        consumer["certificate_directory"] = target["certificate_directory"]
    elif kind == "npm":
        consumer["discover_covered_hosts"] = bool(target.get("discover_covered_hosts"))
    elif kind == "cpanel":
        # Named here only if this certificate is the one the target lists them
        # for. Otherwise empty, which the controller reads as "every site that
        # serves a verified name" once it has asked the account for its sites,
        # so the install list is derived from the verify list and cannot
        # disagree with it.
        consumer["install_domains"] = (
            list(target.get("install_domains") or []) if owns_the_name else []
        )
    return consumer


def _publication_at(
    target: dict[str, Any], *, certificate_name: str
) -> dict[str, Any]:
    """Where one certificate's facts are written at one 1Password target.

    The vault and the item are the target's, and that is deliberately all of it.
    Nothing here describes the content: which facts get published is the
    adapter's fixed decision, so there is no field on this shape for a
    declaration to fill in and nothing for one to redirect.
    """

    return {
        "kind": "onepassword",
        "name": f"{certificate_name}-onepassword",
        "connection_ref": target["connection_ref"],
        "vault": target["vault"],
        "item": target["item"],
    }


def _resolve_tls(
    authored: dict[str, Any], context: ProviderResolutionContext
) -> dict[str, Any]:
    domains = list(authored["domains"])
    targets = [
        _delivery_target(connection_ref, context)
        for connection_ref in authored["install_on"]
    ]
    consumers = [target for target in targets if target["kind"] != "onepassword"]
    if not consumers:
        # Said here rather than left to the resolved model, which would report
        # an empty list and not why it is empty. Recording a certificate is not
        # installing one, so a certificate whose only target records it has
        # nowhere to go, and nothing HQ could observe to confirm it arrived.
        raise ValueError(
            f"{authored['certificate_name']} has no install target. Add one "
            "that serves it."
        )
    return {
        "certificate_name": authored["certificate_name"],
        "domains": domains,
        "consumers": [
            _consumer_at(
                target,
                certificate_key=context.resource_key,
                certificate_name=authored["certificate_name"],
                domains=domains,
                names_at=context.names_at,
            )
            for target in consumers
        ],
        "publish_to": [
            _publication_at(target, certificate_name=authored["certificate_name"])
            for target in targets
            if target["kind"] == "onepassword"
        ],
        "renewal_window_days": authored["renewal_window_days"],
    }


# Delivery target kinds an uploaded certificate cannot go to, and why.
UPLOADED_CERTIFICATE_REFUSALS: Mapping[str, str] = MappingProxyType(
    {
        "cpanel": (
            "Uploaded certificates cannot be installed on cPanel. It "
            "rejects certificates signed by a private CA."
        ),
        # Publishing records what a reconcile observed about a certificate HQ
        # issued; an uploaded certificate is not reconciled that way.
        "onepassword": (
            "Uploaded certificates cannot be recorded in 1Password. "
            "Only certificates HQ issues can."
        ),
    }
)


def _resolve_uploaded(
    authored: dict[str, Any], context: ProviderResolutionContext
) -> dict[str, Any]:
    consumers = []
    for connection_ref in authored["install_on"]:
        target = _delivery_target(connection_ref, context)
        refused = UPLOADED_CERTIFICATE_REFUSALS.get(target["kind"])
        if refused:
            raise ValueError(refused)
        consumer = _consumer_at(
            target,
            certificate_key=context.resource_key,
            certificate_name=authored["certificate_name"],
            domains=[],
        )
        # A private certificate covers names no public proxy host serves, so
        # verifying against everything the target covers would check it against
        # hosts it was never meant to reach.
        consumer.pop("discover_covered_hosts", None)
        consumers.append(consumer)
    return {
        "certificate_name": authored["certificate_name"],
        "install_on": authored["install_on"],
        "consumers": consumers,
        "domains": list(authored.get("domains", ())),
    }


# Each reads a *resolved* spec. A certificate's names are authored and survive a
# failed resolution, which is why an unresolvable one still reports what it
# covers; what resolution adds is where it installs.


def _certificate_hostnames(spec: dict[str, Any]) -> tuple[str, ...]:
    return tuple(spec.get("domains", ()))


def _dns_record_answers(spec: dict[str, Any]) -> tuple[str, ...]:
    """Only the record types that name an address.

    A CNAME resolves to another name, and who can reach *that* is that name's
    statement to make rather than this one's.
    """

    if str(spec.get("record_type", "")) not in ("A", "AAAA"):
        return ()
    content = str(spec.get("content", "")).strip()
    return (content,) if content else ()


def _dns_record_hostnames(spec: dict[str, Any]) -> tuple[str, ...]:
    # A TXT record carries policy (an SPF entry, a validation challenge) not
    # a service. Naming one would put a hostname on the board that nothing is
    # expected to serve, and then permanently report it as unserved. The same is
    # true of MX and CAA, which is why the answer comes from the record-type
    # registry rather than from a list of exceptions maintained here.
    record_type = DNS_RECORD_TYPES_BY_ID.get(spec["record_type"])
    if record_type is None or not record_type.declares_service:
        return ()
    return (spec["name"],)


def _stack_hostnames(spec: dict[str, Any]) -> tuple[str, ...]:
    return tuple(spec.get("hostnames") or ())


def _stack_origin(spec: dict[str, Any]) -> str:
    """Where this answers, as the topology names the machine.

    ``_locate`` matches a host by id as readily as by address, so a stack says
    which machine it runs on and never repeats that machine's address. A stack
    with no port answers nothing directly (it is reached through whatever
    fronts it) and returning nothing is the honest form of that.
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
    operator to supply: nothing else knows it.
    """

    return {
        "name": record.get("stack", ""),
        "host": record.get("host", ""),
        "port": record.get("port") or None,
        "compose": record.get("compose", ""),
        "hostnames": list(record.get("hostnames") or ()),
        "connection_ref": record.get("connection_ref", ""),
    }


def _certificate_readout(
    spec: dict[str, Any], status: dict[str, Any]
) -> tuple[tuple[str, str, str], ...]:
    # Compact on purpose. This readout is what a *service* page shows beside a
    # hostname, and there the question is whether this name is covered by
    # something healthy: who issued it and when it runs out. Where it is
    # installed is the certificate's own page's to list; here it made every
    # card beside it as tall as that list.
    return (
        ("Issuer", "", status.get("issuer", "")),
        ("Expires", "", expiry_phrase(status.get("not_after", ""))),
    )


def _dns_record_origin(spec: dict[str, Any]) -> str:
    """Where a record sends the name, when the record itself is the answer.

    An internal name is routed by a proxy, so the proxy declares the origin. A
    public name pointed straight at something (a CNAME to a Pages site, an A
    record to a host) is routed by the record.

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
    # Both sides through the same formatter, so desired and read-back values
    # compare equal.
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
    every field the reconciler sends is captured here, including the ones an
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
    a zone file would use (name, type, value) is that, and it is unique
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
    name of that shape can be a service however it is published. The record
    type cannot tell: ``sig1._domainkey`` is a DKIM delegation published as a
    CNAME.
    """

    return not any(label.startswith("_") for label in str(name).split("."))


def registry_label(kind: str) -> str:
    """What a registered resource or reading kind is called. Never the identifier."""

    provider = PROVIDERS.get(kind)
    if provider is not None and provider.label:
        return provider.label
    if kind in OBSERVATIONS:
        return OBSERVATIONS[kind].label
    return "Unregistered kind"


def caa_parts(content: str) -> tuple[int, str, str] | None:
    """A CAA value as its three parts, or None if it is not one.

    Cloudflare returns a CAA record as one formatted string and accepts it only
    as three fields. HQ stores the string, because that is what a zone file shows
    and what an operator recognises, and splits it here, once, for the
    validator, the canonicaliser and the controller.
    """

    parsed = re.match(_CAA_VALUE_PARTS, str(content))
    if not parsed:
        return None
    return int(parsed.group(1)), parsed.group(2), parsed.group(3)


def normalized_record_content(record_type: str, content: str) -> str:
    """One spelling of a value, so desired and observed can be compared.

    Declared here, beside the record-type registry, and imported by the
    controller. Identity uses it to decide whether a live record is one HQ
    already declares, and the reconciler uses it to decide whether that record
    needs changing, so the two must be one function.

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
        value = normalized_hostname(value)
    if record_type == "CAA":
        parts = caa_parts(value)
        if parts:
            flags, tag, target = parts
            value = f'{flags} {tag} "{target}"'
    return value


def _dns_record_key_hint(spec: dict[str, Any]) -> str:
    """A name an operator would recognise on a list of declarations.

    The record type is in it because a name usually has more than one record and
    "example-com" would collide with itself four times over on a zone apex.
    """

    name = normalized_hostname(spec.get("name", ""))
    record_type = str(spec.get("record_type", "")).strip().lower()
    return f"{name}-{record_type}"


def _dns_record_removal_note(spec: dict[str, Any]) -> str:
    record_type = DNS_RECORD_TYPES_BY_ID.get(str(spec.get("record_type", "")).upper())
    return record_type.removal_impact if record_type else ""


def _zone_identity(spec: dict[str, Any]) -> tuple[str, ...]:
    zone = normalized_hostname(spec.get("zone", ""))
    return (zone,) if zone else ()


def _zone_key_hint(spec: dict[str, Any]) -> str:
    return normalized_hostname(spec.get("zone", ""))


def _zone_readout(
    spec: dict[str, Any], status: dict[str, Any]
) -> tuple[tuple[str, str, str], ...]:
    """What HQ holds about this domain that is not said better elsewhere.

    Record summaries (MX, SPF, DMARC, CAA) come from
    `application.zone_insights` on the domain page; a readout has no database.
    """

    del status
    return (("Served by", spec.get("connection_ref", ""), ""),)


def _zone_from_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "zone": record.get("zone", ""),
        "connection_ref": record.get("connection_ref", ""),
    }


def _network_key_hint(spec: dict[str, Any]) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", str(spec.get("name", "")).lower()).strip("-")


def _network_readout(
    spec: dict[str, Any], status: dict[str, Any]
) -> tuple[tuple[str, str, str], ...]:
    """What was declared. Nothing sweeps a range, so nothing is observed."""

    del status
    return (
        ("Range", "", str(spec.get("cidr", ""))),
        ("Gateway", "", str(spec.get("gateway", "")) or "none"),
        ("Purpose", "", str(spec.get("purpose", ""))),
    )


def _authority_key_hint(spec: dict[str, Any]) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", str(spec.get("name", "")).lower()).strip("-")


def _authority_readout(
    spec: dict[str, Any], status: dict[str, Any]
) -> tuple[tuple[str, str, str], ...]:
    """What was declared, and how long it has left.

    The expiry is the reason this record exists, so it is phrased as the time
    remaining rather than as the date: a date ten years out reads as "fine"
    at a glance for nine of them.
    """

    del status
    expires = str(spec.get("expires_on", ""))
    remaining = ""
    if expires:
        from application.expiry import days_until
        from application.ui import moment

        when = moment(expires)
        days = days_until(when) if when is not None else None
        if days is not None:
            remaining = (
                f"{expires} · {days // 365} years away"
                if days > 730
                else f"{expires} · {days} days away"
                if days > 0
                else f"{expires} · expired"
            )
    return (
        ("Issues", "", str(spec.get("covers", ""))),
        ("Expires", "", remaining or "does not expire"),
        ("Key location", "", str(spec.get("key_location", ""))),
        ("Issued with", "", str(spec.get("issued_with", ""))),
    )


def _machine_key_hint(spec: dict[str, Any]) -> str:
    return str(spec.get("name", ""))


def _machine_readout(
    spec: dict[str, Any], status: dict[str, Any]
) -> tuple[tuple[str, str, str], ...]:
    """What was declared. Whether it answers is the machine page's to say."""

    return (
        ("Purpose", "", str(spec.get("role", ""))),
        ("Addresses", "", ", ".join(spec.get("addresses", ()))),
    )


def _delivery_target_key_hint(spec: dict[str, Any]) -> str:
    return str(spec.get("connection_ref", ""))


def _delivery_target_readout(
    spec: dict[str, Any], status: dict[str, Any]
) -> tuple[tuple[str, str, str], ...]:
    """What was declared about this target. Nothing here is observed.

    A target is a statement about how a place takes a certificate, so there is
    no drift to show: the certificates installed here are what get reconciled,
    and each reports its own arrival.
    """

    settings = {
        "caddy": ("Certificate directory", spec.get("certificate_directory", "")),
        "npm": (
            "Checks all covered hosts",
            "Yes" if spec.get("discover_covered_hosts") else "No",
        ),
        "cpanel": ("Installs", ", ".join(spec.get("install_domains", ()))),
        "onepassword": (
            "Recorded in",
            " in ".join(
                part
                for part in (spec.get("item", ""), spec.get("vault", ""))
                if part
            ),
        ),
    }.get(str(spec.get("kind", "")))
    # Named first because the list beside this shows only the first row, and
    # the name a certificate goes by at the target is the thing an operator
    # recognises: the key already says which connection it is.
    rows = [
        ("Name at target", "", str(spec.get("name", ""))),
        ("Type", "", str(spec.get("kind", ""))),
        ("Connection", "", str(spec.get("connection_ref", ""))),
        (
            "Name used by",
            "",
            str(spec.get("certificate_resource", "")) or "none",
        ),
    ]
    if settings and settings[1]:
        rows.append((settings[0], "", settings[1]))
    if spec.get("verify_domains"):
        rows.append(("Verified at", "", ", ".join(spec["verify_domains"])))
    return tuple(rows)


def _uploaded_certificate_hostnames(spec: dict[str, Any]) -> tuple[str, ...]:
    # The names come from the certificate itself, which HQ reads when it is
    # uploaded. Nothing is declared here, so before HQ has the artifact this
    # covers nothing, which is true.
    return tuple(spec.get("domains", ()))


def _uploaded_certificate_readout(
    spec: dict[str, Any], status: dict[str, Any]
) -> tuple[tuple[str, str, str], ...]:
    return (
        ("Name", spec.get("certificate_name", ""), ""),
        ("Expires", "", expiry_phrase(status.get("not_after", ""))),
    )


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
    # every co.uk: a guess worth making only when there is nothing better.
    labels = context.hostname.split(".")
    zone = context.public_zone or (
        ".".join(labels[-2:]) if len(labels) > 2 else context.hostname
    )
    return {"name": context.hostname, "zone": zone}


def _uploaded_certificate_seed(context: NameContext) -> dict[str, Any]:
    """An uploaded certificate, named after what needs one.

    Seeding the name is the whole of what a hostname can answer here: which
    machines to install it on is a decision, and the certificate itself arrives
    as a file on the same page. It is the certificate option a name no public
    CA will sign (a `.home.arpa` service) can use.
    """

    label = re.sub(r"[^a-z0-9.-]+", "-", context.hostname.lower()).strip("-.")
    return {"certificate_name": label or "certificate"}


def _container_readout(
    spec: dict[str, Any], status: dict[str, Any]
) -> tuple[tuple[str, str, str], ...]:
    # No "Runs on" row: the card carries the machine as a link, and printing it
    # here as text would say it twice in the same box.
    #
    # No "State" row either: what a container is doing comes from the sweep and
    # is on the card above, with its uptime.
    return (("Container", spec.get("name", ""), status.get("container", "")),)


def _container_from_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "connection_ref": record.get("connection_ref", ""),
        "host": record.get("host", ""),
        "name": record.get("name", ""),
    }


def _container_adopts(record: dict[str, Any]) -> bool:
    """A container a compose project created is declared in its compose file.
    One started by hand is declared nowhere, so it waits for a person."""

    return bool(record.get("stack"))


def _container_identity(spec: dict[str, Any]) -> tuple[str, ...]:
    return (spec.get("host", ""), spec.get("name", ""))


def _container_key_hint(spec: dict[str, Any]) -> str:
    host = spec.get("host", "")
    name = spec.get("name", "")
    return re.sub(r"[^a-z0-9-]+", "-", f"{host}-{name}".lower()).strip("-")


def _container_removal_note(spec: dict[str, Any]) -> str:
    return (
        f"HQ stops watching {spec.get('name', 'this container')} and can no "
        "longer start, stop or restart it. The container keeps running."
    )


def _public_dns_applies(context: NameContext) -> str:
    """Whether any connected account holds a zone this name could live in.

    A `.home.arpa` name has no public zone and never will, so offering to publish
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
    into the name's own zone. No zone, no proof, and no certificate: a fact
    knowable now rather than a minute later in a failed job.
    """

    if not context.swept or context.public_zone:
        return ""
    return (
        "No connected DNS account holds this name's zone, so Let's Encrypt "
        "cannot verify it. Upload a certificate instead."
    )


def _tailnet_device_identity(spec: dict[str, Any]) -> tuple[str, ...]:
    name = str(spec.get("name", ""))
    return (name,) if name else ()


# What a tailnet device declaration is *about* its machine, used to qualify its
# key. Not a service facet: a device is not something a hostname is served by,
# so it belongs here rather than in the facet the service composition reads.
TAILNET_FACET = "tailnet"


def _tailnet_device_key_hint(spec: dict[str, Any]) -> str:
    """``<name>-tailnet``, because a machine already answers to ``<name>``.

    Keys are unique across kinds; an aspect of something is keyed by its name
    and the aspect, as ``<name>-dns`` and ``<name>-proxy`` are.
    """
    name = str(spec.get("name", "")).strip()
    return f"{name}-{TAILNET_FACET}" if name else ""


def _tailnet_device_readout(
    spec: dict[str, Any], status: dict[str, Any]
) -> tuple[tuple[str, str, str], ...]:
    """What HQ asked for about this device, beside what the tailnet reports."""

    wanted = "Never expires" if spec.get("key_expiry_disabled") else "Expires"
    observed = ""
    if status:
        observed = (
            "Never expires"
            if status.get("key_expiry_disabled")
            else expiry_phrase(str(status.get("key_expires", "")))
        )
    return (
        ("Device", "", str(spec.get("name", ""))),
        ("Connection", "", str(spec.get("connection_ref", ""))),
        ("Node key", wanted, observed),
    )


def _tailnet_device_from_record(record: dict[str, Any]) -> dict[str, Any]:
    """A tailnet device, as the declaration that would reproduce it.

    Key expiry is read back, not dropped. The daemon reading is described as
    holding "presence and key expiry, which are the two that go wrong quietly",
    and this mapping kept only the name, so every device asserted a
    ``key_expiry_disabled`` no sweep ever confirmed, which is the quiet way it
    goes wrong. Absence of an expiry is the setting rather than an unknown
    date, the same reading a reconcile makes.
    """

    return {
        "name": record.get("name", ""),
        "key_expiry_disabled": not record.get("key_expires"),
    }


_ADMITTED_PROVIDER_DEFINITIONS = dict(
    admit_controller_adapters(CONTROLLER_PROVIDER_ADAPTERS)
)


def _named_page(route: str, resource, field: str) -> str:
    value = (resource.spec or {}).get(field)
    if value:
        try:
            return reverse(route, args=[value])
        except NoReverseMatch:
            pass
    return reverse("control_plane:detail", kwargs={"key": resource.key})


_PROVIDERS = (
    ProviderSpec(
        "tls.certificate",
        "Issues a Let's Encrypt certificate, renews it, and installs it "
        "wherever these names are served.",
        TLSCertificateSpec,
        ResolvedTLSCertificateSpec,
        _resolve_tls,
        actions={
            "reconcile": applies(automatic=True),
            "renew": applies(
                automatic=True,
                verification=ControllerVerification(
                    timeout_seconds=180,
                    interval_seconds=5,
                ),
            ),
        },
        label="TLS certificate",
        applies=_managed_certificate_applies,
        connection_providers=("cloudflare_dns", "ssh"),
        choices="application.provider_choices:certificate_choices",
        advanced_fields=("renewal_window_days",),
        change_effects=(
            (
                "domains",
                "Saving reissues the certificate and redeploys it to every "
                "target. Takes about a minute.",
            ),
        ),
        facet="certificate",
        readout=_certificate_readout,
        hostnames=_certificate_hostnames,
        seed=_certificate_seed,
        covers=True,
        unobserved_reason=(
            "No sweep reads certificates. HQ updates this record when it "
            "issues or installs the certificate."
        ),
        removal_gap=(
            "The controller cannot delete certificates yet, so renewals would "
            "not stop."
        ),
    ),
    ProviderSpec(
        "tls.uploaded_certificate",
        "Installs a certificate you generated elsewhere. HQ keeps a copy so "
        "you can install it on more targets later.",
        UploadedCertificateSpec,
        ResolvedUploadedCertificateSpec,
        _resolve_uploaded,
        actions={
            "reconcile": applies(automatic=True),
            "delete": applies(),
        },
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
        unobserved_reason=(
            "HQ stores the file. Installs are checked when the certificate "
            "is reconciled."
        ),
    ),
    _ADMITTED_PROVIDER_DEFINITIONS["npm.proxy_host"],
    _ADMITTED_PROVIDER_DEFINITIONS["github.delivery"],
    ProviderSpec(
        "portainer.stack",
        "A set of containers on one machine. HQ creates it in Portainer if "
        "it does not exist.",
        PortainerStackSpec,
        actions={
            "reconcile": applies(automatic=True),
            "delete": applies(),
        },
        label="Container stack",
        connection_providers=("portainer",),
        # Which Portainer is folded away with the tuning. There is normally one,
        # the menu selects it, and asking first makes the form open on the
        # question an operator is least likely to have an opinion about.
        advanced_fields=("connection_ref", "environment"),
        removal_note=lambda spec: (
            f"{spec.get('name', 'This stack')} stops running on "
            f"{spec.get('host', 'its machine')}. Anything it serves goes "
            "offline."
        ),
        facet="runtime",
        hostnames=_stack_hostnames,
        origin=_stack_origin,
        seed=_stack_seed,
        readout=_stack_readout,
        from_record=_stack_from_record,
        sample_record={
            "stack": "example-stack",
            "host": "example-host",
            "connection_ref": "example-portainer",
            "compose": "services:\\n  web:\\n    image: example/web:1\\n",
            "hostnames": ["shop.example.com"],
            "port": 3000,
        },
        choices="application.provider_choices:container_stack",
        unobserved_reason=(
            "Observed through its containers, which the sweep reads."
        ),
    ),
    ProviderSpec(
        "portainer.container",
        "A container HQ watches and can start, stop or restart. Its compose "
        "file still defines it.",
        PortainerContainerSpec,
        actions={
            "reconcile": locked(
                "Defined by a compose file outside HQ. HQ can start, stop and restart it."
            ),
            "restart": applies(),
            "start": applies(),
            "stop": applies(),
        },
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
        adopts=_container_adopts,
        sample_record={
            "name": "example-web",
            "host": "example-host",
            "connection_ref": "example-portainer",
            "stack": "example-stack",
        },
        identity=_container_identity,
        key_hint=_container_key_hint,
        removal_note=_container_removal_note,
        # Ports are behind the disclosure because the answer is usually none:
        # Docker reports them, and only a container sharing the machine's
        # network has to be told.
        advanced_fields=("hidden", "on_demand", "holds_docker_socket", "serves_ports", "source"),
        # So a sweep can never confirm it: the field exists for the case Docker
        # publishes nothing.
        #
        # ``hidden`` is HQ's own bookkeeping (whether the machine page folds the
        # row away); Portainer and Docker have nowhere to keep it. ``source``
        # is the operator's word for an image that names no repository.
        unobservable_fields=("serves_ports", "hidden", "on_demand", "holds_docker_socket", "source"),
        declaration_only=True,
        choices="application.provider_choices:container_stack",
    ),
    ProviderSpec(
        "tailscale.device",
        "Settings HQ keeps for one device on your tailnet, such as key "
        "expiry.",
        TailnetDeviceSpec,
        actions={
            "reconcile": applies(
                automatic=True,
                verification=ControllerVerification(
                    timeout_seconds=60,
                    interval_seconds=10,
                ),
            ),
            "approve-routes": applies(),
        },
        label="Tailnet device",
        connection_providers=("tailscale",),
        console=tailscale_machine,
        readout=_tailnet_device_readout,
        from_record=_tailnet_device_from_record,
        # Carries an expiry, so the round-trip guard has something to check
        # rather than passing on a field the fixture never supplied.
        sample_record={
            "name": "example-device",
            "key_expires": "2026-12-01T00:00:00Z",
            "document": "",
        },
        identity=_tailnet_device_identity,
        key_hint=_tailnet_device_key_hint,
        choices="application.provider_choices:tailnet_device",
        # HQ's own bookkeeping: Tailscale does not hold it and no device
        # reading echoes it back, so a declaration setting it would assert a
        # field nothing can confirm.
        unobservable_fields=("connection_ref",),
        # HQ did not create the device and cannot delete it: running
        # `tailscale up` on the machine is what put it there. Removal means HQ
        # stops keeping its settings, as for a watched container. Left False,
        # removal queues a delete this provider has no action for and is
        # refused, leaving the declaration impossible to remove.
        declaration_only=True,
        change_effects=(
            (
                "key_expiry_disabled",
                "Applied on the next reconcile. Turning it off restores key "
                "expiry.",
            ),
        ),
        removal_note=lambda spec: (
            f"HQ stops managing {spec.get('name', 'this device')}. Its current "
            "settings stay as they are."
        ),
    ),
    ProviderSpec(
        "tailscale.policy",
        "The tailnet's access policy. HQ runs the policy's tests before "
        "applying a change.",
        TailnetPolicySpec,
        actions={
            "reconcile": applies(
                verification=ControllerVerification(
                    timeout_seconds=60,
                    interval_seconds=10,
                )
            ),
        },
        label="Tailnet policy",
        connection_providers=("tailscale",),
        hostnames=None,
        # There is one, it came with the tailnet, and nobody adds a second.
        created_from="tailnet",
        # Adopted from the sweep, so the declaration starts byte-identical to
        # the live policy and editing it is editing what is actually there.
        from_record=lambda record: {"document": record.get("document", "")},
        sample_record={"document": ""},
        identity=lambda spec: ("tailnet",),
        key_hint=lambda spec: "tailnet-policy",
        # As for a device: a policy reading returns the document, not how HQ
        # fetched it.
        unobservable_fields=("connection_ref",),
        # One policy per tailnet, which HQ did not create and cannot delete.
        # Removal means HQ stops keeping it, as for a device.
        declaration_only=True,
        # The most valuable single control in the estate: it decides who can
        # reach what, everywhere, and a mistake in it is not confined to one
        # service. A credential that can change this can open the network, so
        # changing it asks a person first.
        requires_approval=True,
        change_effects=(
            (
                "document",
                "Saving records it. Reconciling applies it if the policy's own "
                "tests pass.",
            ),
        ),
        readout=lambda spec, status: (
            ("Grants", "", str(len(status.get("grants", ())) if status else "")),
            ("Groups", "", str(len(status.get("groups", ())) if status else "")),
            ("Tests", "", str(len(status.get("tests", ())) if status else "")),
        ),
    ),
    ProviderSpec(
        "network",
        "An address range. HQ uses it to tell which network an address is "
        "on.",
        NetworkSpec,
        actions={
            "reconcile": locked(
                "HQ does not create subnets. This entry records one."
            ),
        },
        label="Network",
        declaration_only=True,
        hostnames=None,
        readout=_network_readout,
        # No ``from_record``: nothing sweeps a range. What HQ observes are the
        # things that answer inside one.
        key_hint=_network_key_hint,
        removal_note=lambda spec: (
            f"HQ forgets {spec.get('name', 'this network')}. Addresses in it "
            "are no longer linked to a network."
        ),
        unobserved_reason=(
            "A range is a record. There is nothing to observe."
        ),
    ),
    ProviderSpec(
        "pki.authority",
        "A certificate authority you trust, including an offline root. HQ "
        "records it and does not renew it.",
        CertificateAuthoritySpec,
        actions={
            "reconcile": locked(
                "The authority is kept offline. HQ does not reach it."
            ),
        },
        label="Certificate authority",
        declaration_only=True,
        hostnames=None,
        readout=_authority_readout,
        key_hint=_authority_key_hint,
        removal_note=lambda spec: (
            f"HQ forgets {spec.get('name', 'this authority')}. Certificates it "
            "issued are no longer linked to it."
        ),
        unobserved_reason=(
            "Kept offline. There is nothing to observe."
        ),
    ),
    ProviderSpec(
        "machine",
        "A machine no sweep finds, and its addresses. Machines behind "
        "Portainer or another connection are listed already.",
        MachineSpec,
        actions={
            "reconcile": locked(
                "HQ does not create machines. This entry records one."
            ),
        },
        label="Machine",
        home=lambda resource: _named_page("control_plane:machine", resource, "name"),
        unobserved_reason=(
            "What HQ sees of it comes through its connections, on its machine page."
        ),
        declaration_only=True,
        hostnames=None,
        readout=_machine_readout,
        # No ``from_record``: nothing sweeps machines into an inventory, so
        # there is no record to adopt one from. What HQ observes about a machine
        # arrives as containers and connections, which name it in passing.
        key_hint=_machine_key_hint,
        # Half the addresses on a machine are the only record of it (nothing
        # reports the printer on the LAN) and half repeat what the tailnet
        # says. The field has to stay writable for the first kind, and it is
        # also the key that ties HQ's name for a machine to the tailnet's
        # different one, so this marks which is which rather than locking it.
        notes="application.provider_choices:machine_address_notes",
        removal_note=lambda spec: (
            f"HQ forgets {spec.get('name', 'this machine')}. Anything "
            "forwarding to its addresses shows an unknown host."
        ),
    ),
    ProviderSpec(
        "tls.delivery_target",
        "A place certificates are installed, and how they get there. "
        "Certificates can only install on declared targets.",
        TLSDeliveryTargetSpec,
        actions={
            "reconcile": locked(
                "Nothing to reconcile. Reconcile the certificates installed here."
            ),
        },
        label="Certificate target",
        connection_providers=("npm", "ssh", "onepassword"),
        # Nothing to reconcile: this states how a target takes a certificate,
        # and the certificates that install there are what act on it.
        declaration_only=True,
        resolution_input=True,
        hostnames=None,
        readout=_delivery_target_readout,
        # No ``from_record`` for the same reason as a machine: how a place takes
        # a certificate is not something any provider reports, which is exactly
        # why it has to be stated.
        key_hint=_delivery_target_key_hint,
        choices="application.provider_choices:delivery_target",
        removal_note=lambda spec: (
            f"Certificates stop being installed on {spec.get('name', 'this target')}. "
            "Certificates that list it stop resolving."
        ),
        unobserved_reason=(
            "Nothing to observe. Each certificate reports its own install."
        ),
    ),
    _ADMITTED_PROVIDER_DEFINITIONS["caddy.route"],
    _ADMITTED_PROVIDER_DEFINITIONS["adguard.rewrite"],
    ProviderSpec(
        "cloudflare.dns_record",
        "A DNS record anyone on the internet can look up.",
        CloudflareDNSRecordSpec,
        actions={
            "reconcile": applies(automatic=True),
            "delete": applies(),
        },
        label="Public DNS record",
        applies=_public_dns_applies,
        connection_providers=("cloudflare_dns",),
        advanced_fields=("priority", "ttl"),
        public_effect=True,
        facet="dns",
        hostnames=_dns_record_hostnames,
        seed=_dns_record_seed,
        answers=_dns_record_answers,
        readout=_dns_record_readout,
        from_record=_dns_record_from_record,
        fronts=lambda spec: bool(spec.get("proxied")),
        sample_record={
            "zone": "example.com",
            "name": "www.example.com",
            "record_type": "A",
            "content": "203.0.113.10",
            "ttl": 300,
            "proxied": False,
            "priority": None,
        },
        choices="application.provider_choices:dns_record",
        identity=_dns_record_identity,
        key_hint=_dns_record_key_hint,
        origin=_dns_record_origin,
        created_from="zone",
        removal_note=_dns_record_removal_note,
    ),
    ProviderSpec(
        "cloudflare.zone",
        "A domain HQ manages. Only declared domains have their records "
        "managed, even if the connection can see more zones.",
        CloudflareZoneSpec,
        actions={
            "reconcile": locked(
                "A domain has no settings to reconcile."
            ),
        },
        label="Domain",
        home=lambda resource: _named_page("zones:detail", resource, "zone"),
        console=lambda record: cloudflare_dashboard(record, str(record.get("zone", ""))),
        connection_providers=("cloudflare_dns",),
        public_effect=True,
        hostnames=None,
        readout=_zone_readout,
        from_record=_zone_from_record,
        sample_record={"zone": "example.com", "connection_ref": "example-dns"},
        choices="application.provider_choices:zone",
        identity=_zone_identity,
        key_hint=_zone_key_hint,
        declaration_only=True,
        contains=("cloudflare.dns_record", "zone", "zone"),
    ),
)

PROVIDERS = {provider.kind: provider for provider in _PROVIDERS}


def resource_home(resource) -> str:
    """The URL of the page a resource lives on."""
    provider = PROVIDERS.get(resource.kind)
    if provider is not None and provider.home is not None:
        return provider.home(resource)
    return reverse("control_plane:detail", kwargs={"key": resource.key})


def readout_rows(resource) -> tuple[tuple[str, str, str], ...]:
    """``(label, desired, observed)`` as the resource's provider describes it."""

    provider = PROVIDERS.get(resource.kind)
    if provider is None or provider.readout is None:
        return ()
    try:
        return tuple(provider.readout(resource.spec, resource.status or {}))
    except (KeyError, TypeError, ValueError):
        return ()


# Kinds a controller reports as readings rather than as resources; see
# control_plane.observations.
OBSERVATION_KINDS = frozenset(OBSERVATIONS)


@dataclass(frozen=True)
class ObserverAbility:
    """What a connection is carried for when it reconciles nothing.

    Every other ability is derived from a resource kind: a credential exists to
    make some declaration true, so the kind is the record of why it is held.
    A connection that only ever reads has no kind to derive from, and left at
    that it appears on the connections page holding no authority at all, which
    reads as a credential nobody can account for rather than as a reader.

    So a reader declares its ability here, against the resource it answers for.
    The effect is always a read: if something wants to change state it needs a
    kind, and a kind is what the reconcile machinery keys on.
    """

    provider: str
    name: str
    label: str
    summary: str
    subject_resource: str


_OBSERVER_ABILITIES: tuple[ObserverAbility, ...] = (
    ObserverAbility(
        provider="cloudflare_api",
        name="analytics.read",
        label="Site analytics",
        summary=(
            "Reads site traffic (pages, referrers, countries, devices, "
            "browsers, operating systems) and Core Web Vitals."
        ),
        subject_resource="analytics",
    ),
)


def observer_abilities() -> tuple[ObserverAbility, ...]:
    return _OBSERVER_ABILITIES


# A provider a resource can be reconciled through, or an observer can read
# through, without a credential model would be reported as "proof undeclared"
# forever. Refused here, beside the declarations, rather than found on the page.
_unmodelled = sorted(
    (
        {provider for spec in _PROVIDERS for provider in spec.connection_providers}
        | {ability.provider for ability in _OBSERVER_ABILITIES}
    )
    - set(CONNECTION_CREDENTIALS)
)
if _unmodelled:
    raise ValueError(
        "Connection providers without a credential model: "
        f"{', '.join(_unmodelled)}."
    )


if _unattributed := unattributed_kinds(OBSERVATIONS, _PROVIDERS):
    raise ValueError(
        "Kinds read through a per-connection provider must name connection_ref: "
        f"{', '.join(_unattributed)}."
    )


# The kinds other modules name directly. Spelled once here, beside the registry
# that defines them, because a kind mistyped in a filter is a query that finds
# nothing and reports it as an empty world.
CERTIFICATE_KIND = "tls.certificate"
UPLOADED_CERTIFICATE_KIND = "tls.uploaded_certificate"
CADDY_ROUTE_KIND = "caddy.route"
CONTAINER_KIND = "portainer.container"
CONTAINER_STACK_KIND = "portainer.stack"
DELIVERY_TARGET_KIND = "tls.delivery_target"
MACHINE_KIND = "machine"
ZONE_KIND = "cloudflare.zone"
DNS_RECORD_KIND = "cloudflare.dns_record"
TAILNET_KIND = "tailscale.device"
TAILNET_POLICY_KIND = "tailscale.policy"

for _named in (
    CERTIFICATE_KIND,
    UPLOADED_CERTIFICATE_KIND,
    CADDY_ROUTE_KIND,
    CONTAINER_KIND,
    CONTAINER_STACK_KIND,
    DELIVERY_TARGET_KIND,
    MACHINE_KIND,
    ZONE_KIND,
    DNS_RECORD_KIND,
    TAILNET_KIND,
    TAILNET_POLICY_KIND,
):
    if _named not in PROVIDERS:
        raise ValueError(f"{_named!r} is named as a kind but no provider declares it.")


@lru_cache(maxsize=1)
def controller_capability_registry() -> ControllerCapabilityRegistry:
    """What the controller may do, assembled from the providers themselves.

    A provider knows whether the controller can converge it, whether it should
    do so unprompted, and why not when not; those are properties of the thing,
    not of a deployment.
    """

    missing = sorted(
        kind for kind, provider in PROVIDERS.items() if not provider.actions
    )
    if missing:
        raise ValueError(
            "Every provider must declare what the controller may do to it. "
            "Missing: " + ", ".join(missing)
        )
    return ControllerCapabilityRegistry(
        schema_version=1,
        capabilities={
            kind: ControllerProviderCapability(actions=dict(provider.actions))
            for kind, provider in PROVIDERS.items()
        },
    )


def controller_id() -> str:
    """Which controller this deployment runs.

    An identity, not a policy: it names one installation, so it arrives from the
    environment rather than from the committed contract beside it.

    Falling back to the machine's own name rather than to a word. This is what
    a sweep files its findings under, so a placeholder would put every container
    on a host called "controller", and both processes that ask run on the host
    network, so both get the same answer without anything being passed between
    them.
    """

    return os.environ.get("HQ_CONTROLLER_ID", "").strip() or os.uname().nodename


def controller_capabilities() -> dict[str, Any]:
    """Return the one validated, JSON-safe controller contract."""

    contract = controller_capability_registry().model_dump(mode="json")
    contract["controller_id"] = controller_id()
    return contract


def enabled_controller_actions(
    *, automatic_only: bool = False
) -> tuple[tuple[str, str], ...]:
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
        return False, policy.reason or "The controller cannot run this action."
    return True, "The controller can run this action."


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
                # Said out loud in the contract, so a caller can know before it
                # calls that this kind waits for a person, rather than
                # discovering it from the answer to a change it has already
                # asked for.
                "requires_approval": provider.requires_approval,
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
