"""What the public internet says about a name or an address.

Two questions HQ cannot answer from the inside. A resolver on this network
follows the internal rewrites, so it reports the opposite of what the world
sees; ``application.zones.public_answers_for`` covers the names HQ publishes,
this covers every other name. And who a public address belongs to, which only
the registries know.

Both gateways are injected defaults, so no test here opens a socket. The
handlers are registered as ``read`` capabilities, so the browser form, the
machine API, the CLI and MCP all reach this one implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
import json
import re
from typing import Any, Callable

from control_plane.dns_lookup import LookupUnavailable, registry, resolve

from django.utils.timezone import now

from .projection import iso
from .security import Capability, Principal

# A hostname, conservatively. The value becomes a query parameter on a URL the
# gateway owns, so it cannot change the shape of the request -- but a name that
# is not a name is a question not worth asking, and refusing it here means the
# provider never sees whatever was actually typed.
HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
    r"(?:\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\.?$"
)

# The types HQ asks for, in the order a person reads them. The resolver matches
# these case-sensitively and answers an unknown one with an empty list rather
# than an error, so an unrecognised type would be indistinguishable from a name
# with no records. Fixing the set here means that cannot happen.
RECORD_TYPES = ("A", "AAAA", "CNAME", "MX", "TXT", "NS", "SOA", "CAA")


@dataclass(frozen=True)
class NameCommand:
    """A name to ask a public resolver about."""

    name: str


@dataclass(frozen=True)
class AddressCommand:
    """An address to ask the public registries about.

    ``refresh`` asks again rather than reading the stored answer. The default
    is the stored one: an allocation moves between organisations rarely and a
    PTR record changes when somebody reconfigures a network, so re-asking per
    page load spends a stranger's rate limit to hear the same thing -- and
    discloses, every time, which addresses HQ is interested in.
    """

    address: str
    refresh: bool = False


def _flatten(data: Any) -> str:
    """One record's value as a line of text.

    The resolver types `data` differently per record type -- a string for A and
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
        # property is which tag is present rather than a fixed field. Matching
        # only `issue` left every `issuewild` rendering as raw JSON beside its
        # readable twin.
        for tag in ("issue", "issuewild", "iodef", "contactemail"):
            if tag in data:
                return f"{data.get('critical', 0)} {tag} \"{data[tag]}\""
        if "nsname" in data:
            return f"{data.get('nsname', '')} {data.get('hostmaster', '')}".strip()
        return json.dumps(data, sort_keys=True)
    return str(data)


def _registrant(payload: dict) -> str:
    """The organisation an RDAP entity list names as holding the allocation.

    RDAP carries contacts as jCard, which is an array-of-arrays with the useful
    string three levels down. Read defensively rather than indexed: registries
    differ in how much they publish, and a missing contact is normal.
    """

    for entity in payload.get("entities") or ():
        if not isinstance(entity, dict):
            continue
        if "registrant" not in (entity.get("roles") or ()):
            continue
        card = entity.get("vcardArray")
        rows = card[1] if isinstance(card, list) and len(card) > 1 else ()
        for row in rows if isinstance(rows, list) else ():
            if isinstance(row, list) and len(row) > 3 and row[0] == "fn":
                return str(row[3])
    return ""


def look_up_name(
    command: NameCommand,
    *,
    principal: Principal,
    expected_updated_at: str | None = None,
    resolver: Callable[..., dict] | None = None,
) -> dict[str, Any]:
    """What a resolver outside this network returns for a name."""

    del expected_updated_at
    principal.require(Capability.LOOK_UP_PUBLIC_RECORDS)
    wanted = str(command.name or "").strip().lower().rstrip(".")
    if not wanted or not HOSTNAME.match(wanted):
        raise ValueError("That is not a hostname.")
    # Resolve at execution time so scoped replacements also cover registry calls.
    resolver = resolver or resolve
    payload = resolver("api/dns", {"domain": wanted, "types": ",".join(RECORD_TYPES)})
    records = payload.get("records")
    server = payload.get("server")
    answers = [
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


def look_up_address(
    command: AddressCommand,
    *,
    principal: Principal,
    expected_updated_at: str | None = None,
    resolver: Callable[..., dict] = resolve,
    allocations: Callable[..., dict] = registry,
) -> dict[str, Any]:
    """What the public internet says about one address.

    Two registries, because they answer differently: reverse DNS is published
    by whoever holds the address, RDAP by the registry that allocated it. Both
    are reported rather than reconciled.

    A non-routable address is answered locally rather than asked about. The
    resolver would attempt it, find nothing, and return a body that looks like
    an answer -- having been told an address from inside the estate to get
    there.
    """

    from control_plane.models import AddressReading

    del expected_updated_at
    principal.require(Capability.LOOK_UP_PUBLIC_RECORDS)
    try:
        parsed = ip_address(str(command.address or "").strip())
    except ValueError as exc:
        raise ValueError("That is not an IP address.") from exc
    if not command.refresh:
        stored = AddressReading.objects.filter(address=str(parsed)).first()
        if stored is not None:
            return {**stored.reading, "observed_at": iso(stored.observed_at)}
    if not parsed.is_global:
        return {
            "ok": True,
            "address": str(parsed),
            "version": parsed.version,
            "hostnames": [],
            "note": "This address is not routable on the public internet, so "
            "nothing out there can say anything about it and HQ does not ask.",
        }

    reading: dict[str, Any] = {
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
        answer = resolver("api/reverse-dns", {"ip": str(parsed)})
    except LookupUnavailable:
        reading["note"] = "Reverse DNS could not be read."
    else:
        hostnames = answer.get("hostnames")
        found = [
            str(item)
            for item in (hostnames if isinstance(hostnames, list) else ())
            if item
        ]
        server = answer.get("server")
        reading["hostnames"] = found
        reading["arpa"] = str(answer.get("arpaName") or "")
        reading["resolver"] = (
            str(server.get("name", "")) if isinstance(server, dict) else ""
        )
        # A 200 carrying an `error` key is this resolver reporting "resolved
        # fine, found nothing". Carried as a note rather than raised: no PTR
        # record is a fact about the address, not a failed lookup.
        if not found:
            reading["note"] = str(answer.get("error") or "No PTR record exists.")

    try:
        held = allocations(str(parsed))
    except LookupUnavailable:
        reading["allocation"] = {}
    else:
        prefixes = [
            f"{item.get('v4prefix') or item.get('v6prefix')}/{item.get('length')}"
            for item in (held.get("cidr0_cidrs") or ())
            if isinstance(item, dict)
        ]
        reading["allocation"] = {
            "organisation": _registrant(held),
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
    # Written even when a registry failed. A partial answer is still worth not
    # asking for again a second later, and `refresh` is how an operator says
    # the stored one is not good enough.
    AddressReading.objects.update_or_create(
        address=str(parsed),
        defaults={"reading": reading, "observed_at": now()},
    )
    return {**reading, "observed_at": iso(now())}
