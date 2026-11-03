"""What the public internet says about a name or an address.

Two questions HQ cannot answer from the inside: what the world resolves any
name to, since a resolver here follows the internal rewrites
(``application.zones.public_answers_for`` covers the names HQ publishes), and
who a public address belongs to, which only the registries know. The handlers
are ``read`` capabilities, so every interface reaches this one implementation.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from django.utils.timezone import now

from hq.domains.control_plane.dns_lookup import LookupUnavailable, registry, resolve
from hq.domains.control_plane.names import normalized_hostname
from hq.platform.core.outbound import allowed
lazy from hq.domains.control_plane.models import AddressReading

from .answer_fields import ObservedAt
from .command_fields import Describe
from .projection import iso
from .reach import is_documentation, is_public, network_of
from .record_errors import InputError
from .security import Capability, Principal

# A hostname, conservatively. The value becomes a query parameter on a URL the
# gateway owns, so it cannot change the shape of the request, but a name that
# is not a name is a question not worth asking, and refusing it here means the
# provider never sees whatever was actually typed.
HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
    r"(?:\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\.?$"
)

# The types HQ asks for, in the order a person reads them. The resolver matches
# these case-sensitively and answers an unknown one with an empty list rather
# than an error, so an unrecognized type would be indistinguishable from a name
# with no records. Fixing the set here means that cannot happen.
RECORD_TYPES = ("A", "AAAA", "CNAME", "MX", "TXT", "NS", "SOA", "CAA")

LOOK_UP_NAME = "lookup.name.read"
LOOK_UP_ADDRESS = "lookup.address.read"


@dataclass(frozen=True, slots=True)
class NameCommand:
    """A name to ask a public resolver about."""

    name: Annotated[str, Describe("The hostname to resolve.")]


@dataclass(frozen=True, slots=True)
class AddressCommand:
    """An address to ask the public registries about.

    ``refresh`` asks again rather than reading the stored answer. The default
    is the stored one: an allocation moves between organizations rarely and a
    PTR record changes when somebody reconfigures a network, so re-asking per
    page load spends a stranger's rate limit to hear the same thing, and
    discloses, every time, which addresses HQ is interested in.
    """

    address: Annotated[str, Describe("The IP address to look up.")]
    refresh: Annotated[bool, Describe("Ask again rather than reading the stored answer.")] = False


def _flatten(data: Any) -> str:
    """One record's value as a line of text.

    The resolver types `data` differently per record type: a string for A and
    TXT, an object for MX, CAA and SOA. Flattened once here so no surface
    downstream branches on record type to render a row, and so a type added
    later degrades to readable JSON rather than an exception.
    """

    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        if {"priority", "exchange"} <= data.keys():
            return f"{data['priority']} {data['exchange']}".strip()
        # CAA carries one tag per record and the tag name is the key, so the
        # property is which tag is present rather than a fixed field. Every
        # tag is matched, so `issuewild` renders as readably as `issue`.
        for tag in ("issue", "issuewild", "iodef", "contactemail"):
            if tag in data:
                return f'{data.get("critical", 0)} {tag} "{data[tag]}"'
        if "nsname" in data:
            return f"{data.get('nsname', '')} {data.get('hostmaster', '')}".strip()
        return json.dumps(data, sort_keys=True)
    return str(data)


def entity_name(payload: dict, role: str) -> str:
    """The name an RDAP entity list gives the entity holding ``role``.

    RDAP carries contacts as jCard, which is an array-of-arrays with the useful
    string three levels down. Read defensively rather than indexed: registries
    differ in how much they publish, and a missing contact is normal.
    """

    for entity in payload.get("entities") or ():
        if not isinstance(entity, dict):
            continue
        if role not in (entity.get("roles") or ()):
            continue
        card = entity.get("vcardArray")
        rows = card[1] if isinstance(card, list) and len(card) > 1 else ()
        for row in rows if isinstance(rows, list) else ():
            if isinstance(row, list) and len(row) > 3 and row[0] == "fn":
                return str(row[3])
    return ""


def _registrant(payload: dict) -> str:
    """The organization an RDAP entity list names as holding the allocation."""

    return entity_name(payload, "registrant")


class Allocation(TypedDict, total=False):
    """Who an address was allocated to, as the registry that allocated it says; empty when it could not be read."""

    organization: Annotated[str, Describe("The organization it was allocated to.")]
    name: Annotated[str, Describe("The registry's name for the allocation.")]
    handle: Annotated[str, Describe("The registry's handle for the allocation.")]
    country: Annotated[str, Describe("The country the registry files it under, as a two-letter code.")]
    type: Annotated[str, Describe("The type of allocation, as the registry says it.")]
    range: Annotated[str, Describe("The first and last address of the allocation.")]
    prefixes: Annotated[list[str], Describe("The allocation as CIDR prefixes.")]


def allocation_of(held: dict) -> Allocation:
    """An RDAP address answer as the fields HQ keeps."""

    prefixes = [
        f"{item.get('v4prefix') or item.get('v6prefix')}/{item.get('length')}"
        for item in (held.get("cidr0_cidrs") or ())
        if isinstance(item, dict)
    ]
    return {
        "organization": _registrant(held),
        "name": str(held.get("name") or ""),
        "handle": str(held.get("handle") or ""),
        "country": str(held.get("country") or ""),
        "type": str(held.get("type") or ""),
        "range": " – ".join(
            part
            for part in (
                str(held.get("startAddress") or ""),
                str(held.get("endAddress") or ""),
            )
            if part
        ),
        "prefixes": prefixes,
    }


class NameAnswer(TypedDict):
    """One record a resolver returned for a name."""

    type: Annotated[str, Describe("The record's type, such as `A` or `MX`.")]
    name: Annotated[str, Describe("The name the record is for.")]
    value: Annotated[str, Describe("What the record holds, in one line.")]


class NameLookedUp(TypedDict):
    """What a resolver outside this network returns for a name."""

    ok: Literal[True]
    name: Annotated[str, Describe("The hostname asked about, as HQ normalized it.")]
    resolver: Annotated[str, Describe("The public resolver that answered.")]
    answers: Annotated[list[NameAnswer], Describe("Each record the resolver returned.")]
    resolves: Annotated[bool, Describe("Whether the name has any record at all.")]


def look_up_name(
    command: NameCommand,
    *,
    principal: Principal,
    resolver: Callable[..., dict] | None = None,
) -> NameLookedUp:
    """What a resolver outside this network returns for a name."""

    del principal
    wanted = normalized_hostname(command.name or "")
    if not wanted or not HOSTNAME.match(wanted):
        raise InputError("That is not a hostname.")
    # The gateways are injected defaults, so no test here opens a socket.
    # Resolve at execution time so scoped replacements also cover registry calls.
    resolver = resolver or resolve
    with allowed("lookup"):
        payload = resolver("api/dns", {"domain": wanted, "types": ",".join(RECORD_TYPES)})
    records = payload.get("records")
    server = payload.get("server")
    answers: list[NameAnswer] = [
        {
            "type": str(item.get("type", "")),
            "name": str(item.get("name", "")),
            "value": _flatten(item.get("data")),
        }
        for item in (records if isinstance(records, list) else ())
        if isinstance(item, dict) and item.get("type")
    ]
    # The TTL this resolver reports is 300 on every record of every name, which
    # is not a TTL. Dropped rather than shown: a number that never varies reads
    # as measured and is not.
    return {
        "ok": True,
        "name": wanted,
        "resolver": str(server.get("name", "")) if isinstance(server, dict) else "",
        "answers": answers,
        "resolves": bool(answers),
    }


# What an address nobody on the internet can describe is, said as precisely
# as HQ's own ranges allow.
_UNROUTED = {
    "tailnet": "This is a tailnet address. Only devices on the tailnet can reach it.",
    "network": "This is a private address. Nothing on the public internet knows it.",
    "loopback": "This is a loopback address. It names the machine it is used on.",
}


def _unrouted(address: str) -> str:
    if is_documentation(address):
        return "This address is reserved for documentation. Nothing on the public internet answers at it."
    return _UNROUTED.get(network_of(address), "This address is not on the public internet, so nothing there knows it.")


def _address_answer(command: AddressCommand, *, principal: Principal) -> tuple[Any, dict[str, Any] | None]:
    """The parsed address and what HQ can say about it without asking anyone.

    A stored reading, or for a non-routable address the local answer; None
    when only the registries could say.
    """

    principal.require(Capability.LOOK_UP_PUBLIC_RECORDS)
    try:
        parsed = ip_address(str(command.address or "").strip())
    except ValueError as exc:
        raise InputError("That is not an IP address.") from exc
    if not is_public(str(parsed)):
        return parsed, {
            "ok": True,
            "address": str(parsed),
            "version": parsed.version,
            "hostnames": [],
            "note": _unrouted(str(parsed)),
        }
    stored = AddressReading.objects.filter(address=str(parsed)).first()
    if stored is not None:
        return parsed, {**stored.reading, "observed_at": iso(stored.observed_at)}
    return parsed, None


def stored_address(command: AddressCommand, *, principal: Principal) -> dict[str, Any] | None:
    """What HQ already holds about an address. Asks no one and writes nothing."""

    return _address_answer(command, principal=principal)[1]


class AddressLookedUp(TypedDict):
    """What the public internet says about one address, or HQ where it is not public."""

    ok: Literal[True]
    address: Annotated[str, Describe("The address asked about, as HQ normalized it.")]
    version: Annotated[int, Describe("Which IP version it is, 4 or 6.")]
    hostnames: Annotated[list[str], Describe("The names its reverse DNS gives it.")]
    note: Annotated[
        str,
        Describe("What HQ or a registry says beside the answer, such as that no PTR record exists; empty for none."),
    ]
    arpa: NotRequired[Annotated[str, Describe("Its reverse DNS name; absent when that could not be read.")]]
    resolver: NotRequired[
        Annotated[str, Describe("The resolver that answered for its reverse DNS; absent when that could not be read.")]
    ]
    allocation: NotRequired[Allocation]
    observed_at: NotRequired[ObservedAt]


def look_up_address(
    command: AddressCommand,
    *,
    principal: Principal,
    resolver: Callable[..., dict] | None = None,
    allocations: Callable[..., dict] | None = None,
) -> AddressLookedUp:
    """What the public internet says about one address.

    Two registries, because they answer differently: reverse DNS is published
    by whoever holds the address, RDAP by the registry that allocated it. Both
    are reported rather than reconciled.
    """

    # A non-routable address is answered locally rather than asked about: the
    # resolver would find nothing and return a body that looks like an answer,
    # having been told an address from inside the estate to get there.
    parsed, known = _address_answer(command, principal=principal)
    # Resolved at execution time, as look_up_name's is.
    resolver = resolver or resolve
    allocations = allocations or registry
    if known is not None and (not command.refresh or not is_public(str(parsed))):
        return known
    reading: AddressLookedUp = {
        "ok": True,
        "address": str(parsed),
        "version": parsed.version,
        "hostnames": [],
        "note": "",
    }
    # Each registry is asked independently and neither is allowed to take the
    # other down with it. One answering is a better panel than none, and which
    # one failed is visible in what is missing.
    try:
        with allowed("lookup"):
            answer = resolver("api/reverse-dns", {"ip": str(parsed)})
    except LookupUnavailable:
        reading["note"] = "Reverse DNS could not be read."
    else:
        hostnames = answer.get("hostnames")
        found = [str(item) for item in (hostnames if isinstance(hostnames, list) else ()) if item]
        server = answer.get("server")
        reading["hostnames"] = found
        reading["arpa"] = str(answer.get("arpaName") or "")
        reading["resolver"] = str(server.get("name", "")) if isinstance(server, dict) else ""
        # A 200 carrying an `error` key is this resolver reporting "resolved
        # fine, found nothing". Carried as a note rather than raised: no PTR
        # record is a fact about the address, not a failed lookup.
        if not found:
            reading["note"] = str(answer.get("error") or "No PTR record exists.")

    try:
        with allowed("lookup"):
            held = allocations(str(parsed))
    except LookupUnavailable:
        reading["allocation"] = {}
    else:
        reading["allocation"] = allocation_of(held)
    # Written even when a registry failed. A partial answer is still worth not
    # asking for again a second later, and `refresh` is how an operator says
    # the stored one is not good enough.
    AddressReading.objects.update_or_create(
        address=str(parsed),
        defaults={"reading": reading, "observed_at": now()},
    )
    return {**reading, "observed_at": now().isoformat()}
