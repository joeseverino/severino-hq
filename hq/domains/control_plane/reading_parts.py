"""The parts a kind is read in, and a refused part as a sweep stores it.

A read can succeed while one part of it is refused: a zone's redirect rules
while its page rules read. Each part is declared with the permissions it needs.
A refused part is stored beside the kind's records as structure (which part,
why, and on what), never as a record or a field of one, so a record count, a
credential's sight, a subject's facts and a request path all see one refusal.

The whole kind on one scope is the part ``WHOLE``: a zone whose certificate
packs are refused while other zones read.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any

from .connection_kinds import CONNECTION_LABELS
from .names import in_zone, normalized_hostname
from .observations import OBSERVATIONS, ReadingPart
from .provider_adapters.contracts import (
    CREDENTIAL_REFUSAL,
    PERMISSION_REFUSAL,
)
lazy from .providers import PROVIDERS

WHOLE = ""

_REASON_LENGTH = 300
_SCOPE_LENGTH = 253
_REF_LENGTH = 160

# The request a provider's reason names ("GitHub 403 for /repos/a/b/..."): the
# scope already says which, and without it equal reasons group.
_REQUEST = re.compile(r"\s+for\s+(?:/|https?://)\S+$")
# A reason that is only the status a provider answered ("GitHub 404", "provider
# answered 403"), after whatever led to it.
_STATUS = re.compile(r"(?P<lead>.*:\s+)?(?:provider answered|[A-Z][\w.-]*)\s+(?P<code>[1-5]\d\d)")
# What each status means to the owner, by code or by its hundred.
_STATUS_MEANS = {
    401: "{who}'s credential was refused",
    403: "closed to {who}",
    404: "hidden from {who}",
    429: "{who} was asked to slow down; the next read tries again",
    500: "the service failed to answer; the next read tries again",
    400: "the service refused the read",
}


def parts_of(kind: str) -> Mapping[str, ReadingPart]:
    """Every part of ``kind`` by name, ``WHOLE`` first."""

    spec = OBSERVATIONS.get(kind)
    if spec is not None:
        whole = ReadingPart(WHOLE, spec.label, tuple(spec.requires))
        declared = spec.parts
    else:
        provider = PROVIDERS.get(kind)
        whole = ReadingPart(WHOLE, (provider.label if provider else "") or kind)
        declared = provider.parts if provider else ()
    return frozendict({WHOLE: whole, **{part.name: part for part in declared}})


@dataclass(frozen=True, slots=True)
class PartRefusal:
    """One part of one kind that a sweep could not read, and on what."""

    kind: str
    part: ReadingPart
    # One of ``FAILURES``, or "" when the cause is unknown.
    refusal: str
    reason: str = ""
    # The zone or hostname it was refused on, a record's name, a machine, or
    # "" for all.
    scope: str = ""
    connection_ref: str = ""
    # The machine's address, when the scope is a machine.
    address: str = ""

    @property
    def missing(self) -> tuple[str, ...]:
        """The permissions to add, when a permission was refused."""

        return self.part.requires if self.refusal == PERMISSION_REFUSAL else ()

    @property
    def said(self) -> str:
        """Why it was not read, in words: the provider's reason, with a bare
        HTTP status said as what it means and without the request it named."""

        reason = _REQUEST.sub("", self.reason or "the read failed").rstrip(".")
        status = _STATUS.fullmatch(reason)
        if status is None:
            return reason
        provider = self.part.provider or getattr(OBSERVATIONS.get(self.kind), "provider", "")
        meaning = _STATUS_MEANS.get(int(status["code"]), _STATUS_MEANS.get(int(status["code"]) // 100 * 100, ""))
        return (status["lead"] or "") + meaning.format(who=f"HQ's {CONNECTION_LABELS.get(provider, 'connection')}")

    @property
    def phrase(self) -> str:
        """ "<part> not read: missing <permissions>", or why it was not read."""

        if self.missing:
            return f"{self.part.label} not read: missing {', '.join(self.missing)}"
        return f"{self.part.label} not read: {self.said}"

    def covers(self, hostname: str) -> bool:
        """Whether this refusal hides something about ``hostname``."""

        name = normalized_hostname(hostname)
        return not self.scope or name == self.scope or in_zone(name, self.scope)

    def holds(self, addresses: Iterable[str]) -> bool:
        """Whether this refusal is scoped to a machine at one of ``addresses``."""

        return bool(self.address) and self.address in addresses


def clean_refused_parts(kind: str, reported: Iterable[Mapping[str, Any]] | None) -> list[dict[str, str]]:
    """The contract's ``RefusedPart``s a controller reported, as stored: the
    parts this kind declares, with bounded text. A part it does not declare is
    dropped."""

    parts = parts_of(kind)
    found: list[dict[str, str]] = []
    for entry in reported or ():
        if entry["part"] not in parts:
            continue
        cleaned = {
            "part": entry["part"],
            "refusal": entry["refusal"],
            "reason": entry["reason"][:_REASON_LENGTH],
            "scope": entry["scope"].strip().lower()[:_SCOPE_LENGTH],
            "connection_ref": entry["connection_ref"][:_REF_LENGTH],
        }
        address = _address(entry.get("address"))
        if address:
            cleaned["address"] = address
        found.append(cleaned)
    return found


def _address(value: Any) -> str:
    """An IP address as stored, or "" for anything else."""

    try:
        return str(ip_address(str(value or "").strip()))
    except ValueError:
        return ""


def refused_parts(snapshot: Any) -> tuple[PartRefusal, ...]:
    """The parts a stored snapshot says were refused.

    A credential that read the rest of the kind is valid, so its refusal of a
    part it reads itself is a missing permission. A part another provider's
    credential reads keeps its own refusal.
    """

    parts = parts_of(snapshot.kind)
    found = []
    for entry in _entries(getattr(snapshot, "refused_parts", None)):
        part = parts.get(str(entry.get("part", "")))
        if part is None:
            continue
        refusal = str(entry.get("refusal", "") or "")
        if refusal == CREDENTIAL_REFUSAL and snapshot.reachable and not part.provider:
            refusal = PERMISSION_REFUSAL
        found.append(
            PartRefusal(
                kind=snapshot.kind,
                part=part,
                refusal=refusal,
                reason=str(entry.get("reason", "") or ""),
                scope=str(entry.get("scope", "") or ""),
                connection_ref=str(entry.get("connection_ref", "") or ""),
                address=str(entry.get("address", "") or ""),
            )
        )
    return tuple(found)


def _entries(raw: Any) -> Iterable[Mapping[str, Any]]:
    return [entry for entry in raw or () if isinstance(entry, Mapping)]
