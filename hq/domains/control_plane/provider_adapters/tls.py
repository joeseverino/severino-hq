"""Certificates HQ issues or is handed, and how each reaches its targets."""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Any, Callable, Literal

from pydantic import Field, field_validator, model_validator

from ..names import certificate_covers, normalized_hostname
from ..provider_spec import (
    ControllerVerification,
    NameContext,
    ProviderModel,
    ProviderResolutionContext,
    ProviderSpec,
    applies,
    expiry_phrase,
)


CERTIFICATE_KIND = "tls.certificate"
UPLOADED_CERTIFICATE_KIND = "tls.uploaded_certificate"


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


# A certbot lineage name: it becomes --cert-name and a directory under live/.
CERTIFICATE_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]*$"
# A domain certbot is asked for: DNS labels, optionally under one wildcard.
CERTIFICATE_DOMAIN_PATTERN = (
    r"^(\*\.)?([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)


class TLSCertificateSpec(ProviderModel):
    """One certificate HQ issues, deploys and keeps renewed.

    Everything about it is stated here: what it is called, which names it
    covers, and where it installs. Adding a name is saving the form.

    Titles and descriptions live on the model because the form is generated
    from it.
    """

    certificate_name: str = Field(
        max_length=160,
        pattern=CERTIFICATE_NAME_PATTERN,
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
    def a_certificate_needs_names_and_somewhere_to_go(self) -> TLSCertificateSpec:
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
    def validate_consumers(self) -> ResolvedTLSCertificateSpec:
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


def _uploaded_certificate_seed(context: NameContext) -> dict[str, Any]:
    """An uploaded certificate, named after what needs one.

    Seeding the name is the whole of what a hostname can answer here: which
    machines to install it on is a decision, and the certificate itself arrives
    as a file on the same page. It is the certificate option a name no public
    CA will sign (a `.home.arpa` service) can use.
    """

    label = re.sub(r"[^a-z0-9.-]+", "-", context.hostname.lower()).strip("-.")
    return {"certificate_name": label or "certificate"}


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


CERTIFICATE = ProviderSpec(
    CERTIFICATE_KIND,
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
    choices="hq.platform.application.provider_choices:certificate_choices",
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
)

UPLOADED_CERTIFICATE = ProviderSpec(
    UPLOADED_CERTIFICATE_KIND,
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
    choices="hq.platform.application.provider_choices:uploaded_certificate_choices",
    material_form="hq.platform.application.provider_forms:CertificateUploadForm",
    material_handler="hq.platform.application.certificates:store_uploaded_material",
    facet="certificate",
    hostnames=_uploaded_certificate_hostnames,
    covers=True,
    readout=_uploaded_certificate_readout,
    unobserved_reason=(
        "HQ stores the file. Installs are checked when the certificate "
        "is reconciled."
    ),
)

# Declarations only: the controller half is still the core's.
DEFINITIONS = (CERTIFICATE, UPLOADED_CERTIFICATE)
