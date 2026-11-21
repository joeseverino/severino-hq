"""What HQ records and nothing reconciles: ranges, authorities, machines, targets.

Each is declaration-only. Nothing sweeps a network range or an offline root,
nothing inventories a printer, and how a place takes a certificate is a fact
about the place that no provider reports.
"""

from typing import Annotated, Any, Literal, override

from django.urls.converters import StringConverter
from pydantic import Field, GetJsonSchemaHandler, WithJsonSchema, field_validator, model_validator
from pydantic_core import CoreSchema

from hq.platform.application.command_fields import Reason, RequiredText, checked
from hq.platform.application.labels import dotted
from hq.platform.application.stored_columns import IPV4, IPV6
from hq.platform.application.ui import ended
lazy from hq.platform.application.expiry import days_until
lazy from hq.platform.application.moments import moment, stamp_day

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


# An address, or a network with its prefix length, as ``ipaddress`` reads one.
_CIDR = rf"^(?:{IPV4}(?:/(?:3[0-2]|[12]?[0-9]))?|{IPV6}(?:/(?:12[0-8]|1[01][0-9]|[1-9]?[0-9]))?)$"


class NetworkSpec(ProviderModel):
    """A range of addresses this estate is built on, and what it means.

    Declared, because nothing sweeps a network. HQ learns addresses from the
    things that answer at them (a container's published port, a device's
    tailnet address) and never learns what range they belong to or what that
    range implies. An address on the LAN and one on the tailnet are reachable
    by different people, and only the ranges say which is which.
    """

    name: RequiredText = Field(
        max_length=120,
        title="Name",
        description="A short name for this range.",
    )
    cidr: RequiredText = Field(
        max_length=64,
        pattern=_CIDR,
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
        description=("One line. What uses this range."),
    )


_EXPIRES_ON = Reason("expires_on_day", "must be an ISO date, such as 2036-05-02, or blank")


class CertificateAuthoritySpec(ProviderModel):
    """A certificate authority this estate trusts, and where its key lives.

    Not `tls.certificate`: that is a certificate HQ renews and installs. This is
    the authority a certificate was issued *by*, including one HQ can never
    reach, because the whole point of an offline root is that nothing can. An
    authority nothing sweeps still has an expiry, and an expiry nobody is
    watching is the failure this records.
    """

    name: RequiredText = Field(
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
    expires_on: Annotated[str, WithJsonSchema({"anyOf": [{"const": ""}, {"type": "string", "format": "date"}]})] = (
        Field(
            default="",
            max_length=10,
            title="Expires",
            description="ISO date, e.g. 2036-05-02. Blank if it does not expire.",
        )
    )
    key_location: str = Field(
        default="",
        max_length=300,
        title="Key location",
        description=("Where the private key is stored. Never paste the key."),
    )
    issued_with: str = Field(
        default="",
        max_length=160,
        title="Issued with",
        description="The signing tool, if any.",
    )

    @field_validator("expires_on")
    @classmethod
    @checked
    def a_real_date(cls, value: str) -> str:
        if value and stamp_day(value) is None:
            raise _EXPIRES_ON.refused()
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
    name: RequiredText = Field(
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
        description="Any address it answers on. Whatever forwards to one is linked to this machine.",
    )


class TLSDeliveryTargetSpec(ProviderModel):
    """One place a certificate can be installed, and how it arrives there.

    What a target needs (a Caddy host's directory, the names a cPanel account
    hosts) is true of every certificate it will ever serve, so it is stated
    once here instead of on each certificate that installs there.
    """

    # Flat rather than a union per kind, because the form an operator fills is
    # generated from this model's fields: a union has none, and the four shapes
    # differ by one or two fields each. A ``onepassword`` target receives no
    # certificate: a password manager is where an operator already looks up a
    # credential and when it runs out, so a certificate is *recorded* there,
    # declared and listed like any target, carrying no material
    # (``OnePasswordPublication``).
    kind: Literal["npm", "caddy", "cpanel", "onepassword"] = Field(
        title="Type",
        description="Sets how the certificate is delivered and verified.",
    )
    connection_ref: RequiredText = Field(
        max_length=160,
        title="Connection",
        description="The connection HQ uses to reach this target.",
    )
    name: RequiredText = Field(
        max_length=160,
        title="Name at target",
        description=("The certificate's name on the target. Used only by the certificate below."),
    )
    certificate_resource: str = Field(
        default="",
        max_length=160,
        title="Certificate using this name",
        description=("The certificate that uses the name above. Blank if none."),
    )
    verify_domains: list[str] = Field(
        default_factory=list,
        title="Check these names",
        description=(
            "Names HQ connects to here to confirm the certificate arrived. Leave empty to check every name it covers."
        ),
    )
    certificate_directory: str = Field(
        default="",
        max_length=500,
        title="Certificate directory",
        description=("Caddy only. The directory the certificate and key are written to."),
    )
    discover_covered_hosts: bool = Field(
        default=False,
        title="Check every proxy host it covers",
        description=("Nginx Proxy Manager only. Also check every proxy host this certificate covers."),
    )
    install_domains: list[str] = Field(
        default_factory=list,
        title="Install only on the sites serving these names",
        description=(
            "cPanel only. Each site holds one certificate, shared by its aliases. "
            "Leave empty to install on every site serving a name it is checked at."
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
        description=("1Password only. An existing item HQ writes the certificate's details to."),
    )

    @model_validator(mode="after")
    @checked
    def kind_decides_which_settings_apply(self) -> TLSDeliveryTargetSpec:
        # Refused rather than ignored. A directory typed against an NPM target
        # would sit there looking configured while nothing ever read it.
        for field_name, kind in _ONLY_FOR:
            if getattr(self, field_name) and self.kind != kind:
                raise _ONLY_FOR_REASONS[field_name].refused()
        for kind, needed in _NEEDS.items():
            if self.kind == kind and not all(getattr(self, field_name) for field_name in needed):
                raise _NEEDS_REASONS[kind].refused()
        # Nothing is served here, so there is nothing to connect to and check.
        # Refused rather than ignored, for the same reason as the rest: a name
        # typed here would read as verified and never be probed.
        if self.kind in _SERVES_NOTHING and self.verify_domains:
            raise _SERVES_NOTHING_REASONS[self.kind].refused()
        return self

    @classmethod
    @override
    def __get_pydantic_json_schema__(cls, schema: CoreSchema, handler: GetJsonSchemaHandler) -> dict[str, Any]:
        declared: dict[str, Any] = handler(schema)
        handler.resolve_ref_schema(declared)["anyOf"] = [_target_of(kind) for kind in _KIND_LABELS]
        return declared


# Each setting that applies to one kind of target alone, what each kind needs
# filled, and the kinds that serve nothing to check.
_ONLY_FOR = (
    ("certificate_directory", "caddy"),
    ("discover_covered_hosts", "npm"),
    ("install_domains", "cpanel"),
    ("vault", "onepassword"),
    ("item", "onepassword"),
)
_NEEDS = {"caddy": ("certificate_directory",), "onepassword": ("vault", "item")}
_SERVES_NOTHING = ("onepassword",)
_KIND_LABELS = {"npm": "Nginx Proxy Manager", "caddy": "Caddy", "cpanel": "cPanel", "onepassword": "1Password"}


def _title(name: str) -> str:
    return (TLSDeliveryTargetSpec.model_fields[name].title or name).lower()


def _needs(kind: str) -> str:
    named = (f"{'an' if title[0] in 'aeiou' else 'a'} {title}" for title in map(_title, _NEEDS[kind]))
    return f"is a {_KIND_LABELS[kind]} target, which needs {' and '.join(named)}"


_ONLY_FOR_REASONS = {
    name: Reason(f"tls_target_{name}", f"sets {_title(name)}, which only a {_KIND_LABELS[kind]} target takes")
    for name, kind in _ONLY_FOR
}
_NEEDS_REASONS = {kind: Reason(f"tls_target_{kind}_needs", _needs(kind)) for kind in _NEEDS}
_SERVES_NOTHING_REASONS = {
    kind: Reason(
        f"tls_target_{kind}_checked", f"names a name to check, which a {_KIND_LABELS[kind]} target never serves"
    )
    for kind in _SERVES_NOTHING
}
# A setting left unset, by what it holds.
_UNSET: dict[Any, dict[str, Any]] = {str: {"maxLength": 0}, bool: {"const": False}, list[str]: {"maxItems": 0}}


def _target_of(kind: str) -> dict[str, Any]:
    """What ``kind_decides_which_settings_apply`` holds a target of ``kind``
    to, as a schema branch beside the spec's fields."""

    fields = TLSDeliveryTargetSpec.model_fields
    unset = [name for name, only in _ONLY_FOR if only != kind]
    unset += ["verify_domains"] if kind in _SERVES_NOTHING else []
    needed = _NEEDS.get(kind, ())
    properties: dict[str, Any] = {"kind": {"const": kind}}
    properties |= {name: _UNSET[fields[name].annotation] for name in unset}
    properties |= {name: {"minLength": 1} for name in needed}
    return {"properties": properties, "required": ["kind", *needed]}


def _network_key_hint(spec: dict[str, Any]) -> str:
    return key_from(spec.get("name", ""))


def _network_readout(spec: dict[str, Any], status: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    """What was declared. Nothing sweeps a range, so nothing is observed."""

    del status
    return (
        ("Range", "", str(spec.get("cidr", ""))),
        ("Gateway", "", str(spec.get("gateway", "")) or "none"),
        ("Purpose", "", str(spec.get("purpose", ""))),
    )


def _authority_key_hint(spec: dict[str, Any]) -> str:
    return key_from(spec.get("name", ""))


def _authority_readout(spec: dict[str, Any], status: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    """What was declared, and how long it has left.

    The expiry is the reason this record exists, so it is phrased as the time
    remaining rather than as the date: a date ten years out reads as "fine"
    at a glance for nine of them.
    """

    del status
    expires = str(spec.get("expires_on", ""))
    remaining = ""
    if expires:
        when = moment(expires)
        days = days_until(when) if when is not None else None
        if days is not None:
            remaining = (
                dotted(expires, f"{days // 365} years away")
                if days > 730
                else dotted(expires, f"{days} days away")
                if days > 0
                else dotted(expires, "expired")
            )
    return (
        ("Issues", "", str(spec.get("covers", ""))),
        ("Expires", "", remaining or "does not expire"),
        ("Key location", "", str(spec.get("key_location", ""))),
        ("Issued with", "", str(spec.get("issued_with", ""))),
    )


def _machine_key_hint(spec: dict[str, Any]) -> str:
    return str(spec.get("name", ""))


def _machine_readout(spec: dict[str, Any], _status: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    """What was declared. Whether it answers is the machine page's to say."""

    return (
        ("Purpose", "", str(spec.get("role", ""))),
        ("Addresses", "", ", ".join(spec.get("addresses", ()))),
    )


def _delivery_target_key_hint(spec: dict[str, Any]) -> str:
    return str(spec.get("connection_ref", ""))


def _delivery_target_readout(spec: dict[str, Any], _status: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
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
            " in ".join(part for part in (spec.get("item", ""), spec.get("vault", "")) if part),
        ),
    }.get(str(spec.get("kind", "")))
    # Named first because the list beside this shows only the first row, and
    # the name a certificate goes by at the target is the thing an operator
    # recognizes: the key already says which connection it is.
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
    "An address range. HQ uses it to tell which network an address is on.",
    NetworkSpec,
    actions={
        "reconcile": locked("Saved in HQ only. Nothing changes on the network."),
    },
    label="Network",
    declaration_only=True,
    hostnames=None,
    readout=_network_readout,
    # No ``_from_record``: nothing sweeps a range. What HQ observes are the
    # things that answer inside one.
    key_hint=_network_key_hint,
    removal_note=lambda spec: (
        f"HQ forgets {ended(spec.get('name', 'this network'))} Addresses in it are no longer linked to a network."
    ),
    unobserved_reason=("Kept in HQ only, so there is nothing live to read."),
)

AUTHORITY = ProviderSpec(
    AUTHORITY_KIND,
    "A certificate authority you trust, including an offline root. HQ records it and does not renew it.",
    CertificateAuthoritySpec,
    actions={
        "reconcile": locked("The authority is kept offline. HQ does not reach it."),
    },
    label="Certificate authority",
    declaration_only=True,
    hostnames=None,
    readout=_authority_readout,
    key_hint=_authority_key_hint,
    removal_note=lambda spec: (
        f"HQ forgets {ended(spec.get('name', 'this authority'))} Certificates it issued are no longer linked to it."
    ),
    unobserved_reason=("Kept offline, so there is nothing to read."),
)

MACHINE = ProviderSpec(
    MACHINE_KIND,
    "A machine no connection reports, added by hand with its addresses.",
    MachineSpec,
    actions={
        "reconcile": locked("Saving records it in HQ. Nothing changes on the machine."),
    },
    label="Machine",
    home=lambda resource: named_page("control_plane:machine", resource, "name"),
    unobserved_reason=("What HQ sees of it comes through its connections, on its machine page."),
    declaration_only=True,
    hostnames=None,
    readout=_machine_readout,
    # No ``_from_record``: nothing sweeps machines into an inventory, so
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
        f"HQ forgets {ended(spec.get('name', 'this machine'))} Anything forwarding to its addresses shows an unknown host."
    ),
)

DELIVERY_TARGET = ProviderSpec(
    DELIVERY_TARGET_KIND,
    "A place certificates are installed, and how they get there. Certificates install only on targets added here.",
    TLSDeliveryTargetSpec,
    actions={
        "reconcile": locked("Saved in HQ. Certificates installed here use it when next applied."),
    },
    label="Certificate target",
    connection_providers=("npm", "ssh", "onepassword"),
    # Nothing to reconcile: this states how a target takes a certificate,
    # and the certificates that install there are what act on it.
    declaration_only=True,
    resolution_input=True,
    hostnames=None,
    readout=_delivery_target_readout,
    # No ``_from_record`` for the same reason as a machine: how a place takes
    # a certificate is not something any provider reports, which is exactly
    # why it has to be stated.
    key_hint=_delivery_target_key_hint,
    choices="hq.platform.application.provider_choices:delivery_target",
    removal_note=lambda spec: (
        f"Certificates stop being installed on {ended(spec.get('name', 'this target'))} "
        "HQ flags any certificate that still lists it."
    ),
    unobserved_reason=("Nothing to read. Each certificate shows its own install."),
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
    "onepassword": ConnectionKind(
        "1Password",
        "scoped",
        SERVICE_ACCOUNT,
        create=(
            "A 1Password service account that can write only to the vault certificates are published to. "
            "HQ reads its own secrets through Connect, not this."
        ),
        create_url="https://developer.1password.com/docs/service-accounts/get-started/",
        gives="Renewed certificates published to a vault, for machines that fetch their own.",
    ),
    "ssh": ConnectionKind(
        "SSH",
        "coarse",
        SSH_TRANSPORT,
        create="An SSH key made in 1Password, its public half in the machine's authorized_keys.",
        create_url="https://developer.1password.com/docs/ssh/manage-keys/",
        gives="Caddy routes and certificates on your machines, and which of their ports answer.",
    ),
}
