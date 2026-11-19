"""The shapes the domains resource answers in: each domain's row and record."""

from typing import Annotated, TypedDict

from .answer_fields import ObservedAt, PageUrl
from .describe import Describe, listed
from .managed_records import HealthPart
from .record_parts import WrittenLink
from .tone import Tone
from .ui import ended


class ZoneMemberPart(TypedDict):
    hostname: Annotated[str, Describe("The hostname of a service under the domain.")]
    status: Annotated[str, Describe(ended(f"How that service stands: {listed(Tone)}"))]


class Registration(TypedDict):
    """The domain's registration as last read: when it runs out and whether it renews."""

    expires: Annotated[str, Describe("When the registration runs out, an ISO 8601 date-time.")]
    source: Annotated[str, Describe("The reading the date comes from, in words.")]
    auto_renew: Annotated[bool, Describe("Whether the registrar renews it on its own.")]


class DomainRow(TypedDict):
    name: Annotated[str, Describe("The domain's name, which a read of one names it by.")]
    node: Annotated[str, Describe("The domain's id on the map: `zone:` and its name.")]
    url: PageUrl
    declaration: Annotated[str, Describe("The key of HQ's record of the domain's DNS zone; empty when HQ keeps none.")]
    services: Annotated[list[ZoneMemberPart], Describe("The services under the domain, HQ's own among them.")]
    registration: Registration | None


class DnsRecordPart(TypedDict):
    name: Annotated[str, Describe("The name in the zone the record is for.")]
    record_type: Annotated[str, Describe("The record's type, such as `A`, `CNAME` or `TXT`.")]
    value: Annotated[str, Describe("What the record holds, as the zone lists it.")]
    proxied: Annotated[bool, Describe("Whether the DNS service proxies traffic for it.")]
    ttl: Annotated[int, Describe("How long a resolver may keep it, in seconds; 1 leaves it to the DNS service.")]
    declaration: Annotated[str, Describe("The key of HQ's record of it; empty for none.")]
    observed_only: Annotated[bool, Describe("Whether only the live zone holds it, with no record of HQ's.")]
    manageable: Annotated[bool, Describe("Whether HQ can declare a record of its type; false for one HQ only lists.")]
    secondary: Annotated[
        bool, Describe("Whether it states policy or proves ownership, such as a TXT or CAA record, over routing.")
    ]
    service: Annotated[str, Describe("The hostname of the service it declares; empty for a type that declares none.")]
    health: Annotated[HealthPart | None, Describe("The health of HQ's record of it; null for none.")]
    edits: Annotated[bool, Describe("Whether HQ's record of it offers an edit.")]
    removes: Annotated[bool, Describe("Whether HQ's record of it offers its removal.")]
    summary: Annotated[
        str,
        Describe("Its value in a line, where another record shares its name and type; empty where none does."),
    ]


class InsightRowPart(TypedDict):
    title: Annotated[str, Describe("What the row names.")]
    detail: Annotated[str, Describe("The one fact that sets it apart; empty for none.")]
    url: Annotated[str, Describe("Its page; empty for none.")]
    status: Annotated[str, Describe(f"How it stands: {listed(Tone)}, or empty for no state.")]
    badge: Annotated[str, Describe("That state in words; empty for none.")]


class LinkGroupPart(TypedDict):
    label: Annotated[str, Describe("What the things it names are to the domain, such as `Served by`.")]
    links: Annotated[list[WrittenLink], Describe("The things it names.")]


class InsightPart(TypedDict):
    label: Annotated[str, Describe("What the insight is about, in a few words.")]
    value: Annotated[str, Describe("What HQ found, in a few words.")]
    detail: Annotated[str, Describe("More on it, in a sentence; empty for none.")]
    url: Annotated[str, Describe("Where to look into it; empty for none.")]
    concern: Annotated[bool, Describe("Whether it is a concern to act on.")]
    notice: Annotated[bool, Describe("Whether it is a notice worth knowing, short of a concern.")]
    rows: Annotated[list[InsightRowPart], Describe("The whole list a counted `value` stands for; empty for none.")]
    links: Annotated[list[LinkGroupPart], Describe("What it names, each group under what it is to the domain.")]
    note: Annotated[str, Describe("A reason kept beside it, in a line; empty for none.")]
    note_title: Annotated[str, Describe("The note's full reason, where `note` says it in fewer words; empty for none.")]


class DomainRecord(DomainRow):
    """One domain with its records, its connection and what HQ makes of it."""

    connection_ref: Annotated[str, Describe("The connection that reads the domain's DNS records; empty for none.")]
    observed_only: Annotated[
        bool, Describe("Whether HQ keeps no record of it and reads it through a connection that cannot change it.")
    ]
    adoption: Annotated[
        str,
        Describe(
            "The handle `infrastructure.zone.adopt` takes to manage the domain as it was read; empty when HQ "
            "manages it or cannot."
        ),
    ]
    reachable: Annotated[bool, Describe("Whether that connection answered its last read.")]
    observed_at: ObservedAt | None
    records: Annotated[list[DnsRecordPart], Describe("The domain's DNS records as last read.")]
    insights: Annotated[list[InsightPart], Describe("What HQ makes of the domain.")]
