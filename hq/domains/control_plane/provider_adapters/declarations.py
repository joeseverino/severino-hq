"""What HQ records and nothing reconciles: ranges, authorities, machines, targets.

Each is declaration-only. Nothing sweeps a network range or an offline root,
nothing inventories a printer, and how a place takes a certificate is a fact
about the place that no provider reports.
"""

from typing import Any, Literal

from django.urls.converters import StringConverter
from pydantic import Field, field_validator, model_validator

from ..connection_shapes import SERVICE_ACCOUNT, SSH_TRANSPORT
from ..provider_spec import (
    ConnectionKind,
    ProviderModel,
    ProviderSpec,
    key_from,
    locked,
    named_page,
)


NETWORK_KIND = "network"
AUTHORITY_KIND = "pki.authority"
MACHINE_KIND = "machine"
DELIVERY_TARGET_KIND = "tls.delivery_target"


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
    def kind_decides_which_settings_apply(self) -> TLSDeliveryTargetSpec:
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


def _network_key_hint(spec: dict[str, Any]) -> str:
    return key_from(spec.get("name", ""))


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
    return key_from(spec.get("name", ""))


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
        from hq.platform.application.expiry import days_until
        from hq.platform.application.timestamps import moment

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


NETWORK = ProviderSpec(
    NETWORK_KIND,
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
)

AUTHORITY = ProviderSpec(
    AUTHORITY_KIND,
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
)

MACHINE = ProviderSpec(
    MACHINE_KIND,
    "A machine no sweep finds, and its addresses. Machines behind "
    "Portainer or another connection are listed already.",
    MachineSpec,
    actions={
        "reconcile": locked(
            "HQ does not create machines. This entry records one."
        ),
    },
    label="Machine",
    home=lambda resource: named_page("control_plane:machine", resource, "name"),
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
    notes="hq.platform.application.provider_choices:machine_address_notes",
    removal_note=lambda spec: (
        f"HQ forgets {spec.get('name', 'this machine')}. Anything "
        "forwarding to its addresses shows an unknown host."
    ),
)

DELIVERY_TARGET = ProviderSpec(
    DELIVERY_TARGET_KIND,
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
    choices="hq.platform.application.provider_choices:delivery_target",
    removal_note=lambda spec: (
        f"Certificates stop being installed on {spec.get('name', 'this target')}. "
        "Certificates that list it stop resolving."
    ),
    unobserved_reason=(
        "Nothing to observe. Each certificate reports its own install."
    ),
)

# Declarations only: the controller half is still the core's.
DEFINITIONS = (NETWORK, AUTHORITY, MACHINE, DELIVERY_TARGET)

# The transports HQ's own declarations deliver through: a delivery target is
# reached over SSH or published into a password manager.
CONNECTIONS = {
    # A service account token is issued per vault and per permission, so the
    # one HQ carries can be write access to a single item's vault and nothing
    # else. That is the property the publishing adapter is built to deserve
    # rather than to rely on.
    "onepassword": ConnectionKind("1Password", "scoped", SERVICE_ACCOUNT),
    "ssh": ConnectionKind("SSH", "coarse", SSH_TRANSPORT),
}
