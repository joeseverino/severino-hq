"""Public DNS: the record-type registry, Cloudflare records and the zones they live in."""

import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal, get_args

from pydantic import Field, model_validator

from ..connection_shapes import API_TOKEN
from ..consoles import cloudflare_dashboard
from ..credential_reads import REGISTRAR_READ
from ..names import normalized_hostname
from ..observations.contract import ReadingPart
from ..provider_spec import (
    ConnectionKind,
    NameContext,
    ProviderModel,
    ProviderSpec,
    SharedValue,
    applies,
    locked,
    named_page,
)


DNS_RECORD_KIND = "cloudflare.dns_record"
ZONE_KIND = "cloudflare.zone"


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
    def type_shape(self) -> CloudflareDNSRecordSpec:
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


def _dns_record_name(spec: dict[str, Any]) -> str:
    """A record as a zone file would say it: the name, its type, and for an
    address or an alias where it points."""

    name = normalized_hostname(spec.get("name", ""))
    record_type = str(spec.get("record_type", "")).strip().upper()
    if record_type in ("A", "AAAA", "CNAME") and spec.get("content"):
        return f"{name} → {spec['content']}"
    return f"{name} {record_type}".strip()


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


DNS_RECORD = ProviderSpec(
    DNS_RECORD_KIND,
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
    choices="hq.platform.application.provider_choices:dns_record",
    identity=_dns_record_identity,
    key_hint=_dns_record_key_hint,
    name=_dns_record_name,
    origin=_dns_record_origin,
    created_from="zone",
    removal_note=_dns_record_removal_note,
)

ZONE = ProviderSpec(
    ZONE_KIND,
    "A domain HQ manages. Only declared domains have their records "
    "managed, even if the connection can see more zones.",
    CloudflareZoneSpec,
    actions={
        "reconcile": locked(
            "A domain has no settings to reconcile."
        ),
    },
    label="Domain",
    home=lambda resource: named_page("zones:detail", resource, "zone"),
    console=lambda record: cloudflare_dashboard(record, str(record.get("zone", ""))),
    connection_providers=("cloudflare_dns",),
    public_effect=True,
    hostnames=None,
    readout=_zone_readout,
    from_record=_zone_from_record,
    sample_record={"zone": "example.com", "connection_ref": "example-dns"},
    choices="hq.platform.application.provider_choices:zone",
    identity=_zone_identity,
    key_hint=_zone_key_hint,
    name=_zone_key_hint,
    declaration_only=True,
    contains=(DNS_RECORD_KIND, "zone", "zone"),
    parts=(
        ReadingPart(
            "posture", "Zone TLS posture", ("Zone Settings Read (zone)",), "cloudflare_api"
        ),
        ReadingPart("registration", "Domain registration", (REGISTRAR_READ,), "cloudflare_api"),
    ),
)

# Declarations only: the controller half is still the core's.
DEFINITIONS = (DNS_RECORD, ZONE)

SHARED = (
    SharedValue(
        "CloudflareCAAValue",
        Annotated[str, Field(pattern=_CAA_VALUE_PARTS)],
        "A CAA record's content as flags, tag and quoted value (the three groups). HQ "
        "validates a declared CAA record with it and both sides take the content apart "
        "with it.",
    ),
)

# The connection this provider's credential arrives through, beside its kinds:
# admitting the module admits both.
CONNECTIONS = {
    "cloudflare_api": ConnectionKind("Cloudflare API", "scoped", API_TOKEN),
    "cloudflare_dns": ConnectionKind("Cloudflare DNS", "scoped", API_TOKEN),
}
