"""Certificates a reading reports, near or past expiry, about the connection that read each.

A certificate a proxy serves counts too, since it may be read nowhere else.
"""

from collections.abc import Callable, Hashable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from hq.domains.control_plane.certificate_authorities import UPLOADED
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.providers import PROVIDERS
lazy from hq.domains.control_plane.provider_spec import expiry_phrase

from .action_links import command_url
from .entity_links import node_link
from .expiry import days_until
from .facts import Joined, inventory_records
from .finding_model import Finding, FindingEstate, FindingRemedy, FindingRule, opens
from .labels import lower_first
from .moments import expiry_words, moment
from .references import Kind
from .thresholds import Threshold
from .tone import Tone
from .topology_model import TopologyNode
lazy from .estate import Expiry, managed_certificates
lazy from .topology_estate import machine_nodes, readers_of

# A provider that renews on its own does so with thirty days left; a
# certificate still inside three weeks of expiry missed that renewal.
WARN_DAYS = Threshold(21, "days")
SERIOUS_DAYS = Threshold(7, "days")


@dataclass(frozen=True, slots=True)
class HeldCertificate:
    """One certificate that states an expiry, as a connection holds it."""

    kind: str
    title: str
    expires: str
    names: tuple[str, ...] = ()
    issuer: str = ""


def held(nodes: dict[str, TopologyNode], machine: Callable[[Any], str]) -> tuple[tuple[str, HeldCertificate], ...]:
    """Each certificate that states an expiry, by the node that holds it: each
    a reading lists, then each a proxy serves with that its connection did not
    already list. Placing one makes sure its reader's node stands in ``nodes``.
    """

    readers, holders = readers_of(nodes, machine)
    found: list[tuple[str, HeldCertificate]] = []
    seen: set[tuple[str, str, datetime | None]] = set()
    for certificate, node_ids in (*_read(readers), *_served(holders)):
        for node_id in node_ids:
            key = (node_id, certificate.title, moment(certificate.expires))
            if node_id in nodes and key not in seen:
                seen.add(key)
                found.append((node_id, certificate))
    return tuple(found)


Found = Iterator[tuple[HeldCertificate, tuple[str, ...]]]


def _read(readers) -> Found:
    """Each certificate a reading lists, and the nodes that read it."""

    for kind, spec in OBSERVATIONS.items():
        if spec.facet != "certificate":
            continue
        for snapshot, record in inventory_records(kind):
            expires = spec.expires(record)
            if not moment(expires):
                continue
            joined = Joined(
                spec,
                record,
                str(record.get("connection_ref", "") or ""),
                snapshot.observed_at,
                controller_id=snapshot.controller_id,
            )
            certificate = HeldCertificate(
                kind, spec.title(record), expires, tuple(spec.hostnames(record)), spec.issuer(record)
            )
            yield certificate, tuple(readers(joined))


def _served(holders) -> Found:
    """Each certificate a proxy serves with, every name it serves with it, and
    the nodes the proxy was read through. HQ neither issued nor manages one
    loaded from a file on the proxy's host, so no other reading lists it."""

    found: dict[tuple[str, ...], tuple[set[str], tuple[str, str, str], str]] = {}
    for kind, provider in PROVIDERS.items():
        if provider.served_certificate is None:
            continue
        for _snapshot, record in inventory_records(kind):
            served = provider.served_certificate(dict(record))
            if served is None or served.unread:
                continue
            expires = str(served.certificate.get("expires_on", "") or "")
            if not moment(expires):
                continue
            for node_id in holders(provider, record):
                key = (kind, str(served.certificate.get("name", "")), expires)
                found.setdefault((node_id, *key), (set(), key, served.issuer))[0].update(served.hostnames)
    for (node_id, *_), (names, key, issuer) in found.items():
        yield HeldCertificate(*key, tuple(sorted(names)), issuer), (node_id,)


def _held_on(estate: FindingEstate) -> Iterator[tuple[TopologyNode, HeldCertificate]]:
    """What ``held`` finds, by node in the projection's order."""

    nodes = {node.id: node for node in estate.nodes()}
    by_node: dict[str, list[HeldCertificate]] = {}
    for node_id, certificate in held(dict(nodes), machine_nodes()):
        by_node.setdefault(node_id, []).append(certificate)
    for node_id, node in nodes.items():
        for certificate in by_node.get(node_id, ()):
            yield node, certificate


def serving[T](found: Iterable[tuple[Hashable, datetime, T]]) -> list[T]:
    """Of the certificates ``found``, each with the name it is read for and
    when it expires, the one that serves each name.

    A reading can hold several for one name (an edge certificate beside the
    older one it replaced), and the name is served by the one that runs out
    last; of two that run out together, the first read.
    """

    latest: dict[Hashable, tuple[datetime, T]] = {}
    for name, expires, item in found:
        if name not in latest or expires > latest[name][0]:
            latest[name] = (expires, item)
    return [item for _expires, item in latest.values()]


def expiring(estate: FindingEstate) -> tuple[Finding, ...]:
    """A certificate a connection serves its names with, or one that serves
    none, inside three weeks of expiry or past it, then each HQ renews inside
    its renewal window."""

    return judged(_held_on(estate), estate.now) + renewals_judged(_renewals(estate), estate.now)


def judged(found: Iterable[tuple[Any, HeldCertificate]], now: datetime) -> tuple[Finding, ...]:
    """Each certificate ``found``, by the node that holds it, inside three
    weeks of expiry or past it as of ``now``."""

    held_now = serving(
        (_served_as(node, certificate), when, (node, certificate, days_until(when, now)))
        for node, certificate in found
        if (when := moment(certificate.expires)) is not None
    )
    alike: dict[tuple[str, str, str], list[tuple[Any, HeldCertificate, int]]] = {}
    for node, certificate, days in held_now:
        if days <= WARN_DAYS.value:
            alike.setdefault(_reads_as(certificate, days), []).append((node, certificate, days))
    findings = []
    for group in alike.values():
        said = _telling(group) if len(group) > 1 else None
        findings += [
            _finding(node, certificate, days, apart=said(node, certificate) if said else ("", ""))
            for node, certificate, days in group
        ]
    return tuple(sorted(findings, key=lambda item: item.title))


def _served_as(node, certificate: HeldCertificate) -> tuple[Any, ...]:
    """The name a certificate on ``node`` is judged under: its kind and the
    first name it serves, as the estate's edge certificates are. One that
    serves no name stands for none, so it is judged alone."""

    if certificate.names:
        return (node.id, certificate.kind, certificate.names[0])
    return (node.id, certificate)


def _label(kind: str) -> str:
    spec = OBSERVATIONS.get(kind)
    # A served certificate is known only as what a proxy answers with.
    return spec.label if spec is not None else "Certificate"


def _reads_as(certificate: HeldCertificate, days: int) -> tuple[str, str, str]:
    """What a finding's title says of a certificate before it is told apart."""

    return _label(certificate.kind), certificate.title, expiry_words(days)


def _issued(_node, certificate: HeldCertificate) -> tuple[str, str]:
    issuer = certificate.issuer
    # An uploaded one has no issuer to name, so the word leads the title.
    return (UPLOADED.capitalize(), "") if issuer == UPLOADED else ("", f"from {issuer or 'an unnamed issuer'}")


# What tells two certificates of one title apart, in the order tried: two for
# one name (a zone's edge certificate and its backup) by who issued them or
# what they serve, and one held in two places by where. Each says the words
# that lead the title and those that follow the certificate's name.
Telling = Callable[[Any, HeldCertificate], tuple[str, str]]
_TELLINGS: tuple[Telling, ...] = (
    _issued,
    lambda _node, certificate: ("", f"for {', '.join(certificate.names) or 'no name'}"),
    lambda node, _certificate: ("", f"in {node.label}"),
)


def _telling(group: list[tuple[Any, HeldCertificate, int]]) -> Telling:
    """The first telling that says each of ``group`` differently, else the last."""

    for telling in _TELLINGS:
        if len({telling(node, certificate) for node, certificate, _days in group}) == len(group):
            return telling
    return _TELLINGS[-1]


def _finding(node, certificate: HeldCertificate, days: int, apart: tuple[str, str] = ("", "")) -> Finding:
    """The finding for one certificate with ``days`` left, ``apart`` saying
    what tells it from another whose title would read the same."""

    kind, title, stamp, issuer = certificate.kind, certificate.title, certificate.expires, certificate.issuer
    names = ",".join(certificate.names)
    label, _title, said = _reads_as(certificate, days)
    lead, tail = apart
    label = f"{lead} {lower_first(label)}" if lead else label
    day = when.date().isoformat() if (when := moment(stamp)) else stamp
    return Finding(
        rule="certificate-expiring",
        subject=node.id,
        title=" ".join(filter(None, (label, title, tail, said))),
        # One connection can hold several certificates for one name, each its
        # own matter in the queue. The day stands for the expiry, since a key
        # holds no time stamp (display_words).
        part="|".join((kind, title, day, issuer, names)),
        # One that serves no name breaks nothing when it lapses: it is left
        # over, and the advice is to remove it, not renew it.
        severity=Tone.SERIOUS if names and days <= SERIOUS_DAYS.value else Tone.ATTENTION,
        explanation=_SERVING_EXPLANATION if names else _UNUSED_EXPLANATION,
        evidence=(
            ("Certificate", title),
            ("Held in", node_link(node)),
            ("Expires", expiry_phrase(stamp)),
            *((("Issued by", issuer),) if issuer else ()),
            *((("Serves", ", ".join(certificate.names)),) if names else ()),
        ),
    )


_SERVING_EXPLANATION = "Every name it serves breaks when it expires."
_UNUSED_EXPLANATION = "It serves no name, so delete it."


def _renewals(estate: FindingEstate) -> Iterator[tuple[TopologyNode, Expiry]]:
    """Each certificate HQ renews, with its declaration's node."""

    nodes = {node.id: node for node in estate.nodes()}
    for expiry in managed_certificates():
        if (node := nodes.get(Kind.RESOURCE.of(expiry.resource_key))) is not None:
            yield node, expiry


def renewals_judged(found: Iterable[tuple[Any, Expiry]], now: datetime) -> tuple[Finding, ...]:
    """A certificate HQ renews, inside its renewal window as of ``now``: the renewal has not run."""

    findings = []
    for node, expiry in found:
        if (days := days_until(expiry.expires, now)) > expiry.renewal_window_days:
            continue
        findings.append(
            Finding(
                rule="certificate-expiring",
                subject=node.id,
                severity=Tone.SERIOUS if days <= SERIOUS_DAYS.value else Tone.ATTENTION,
                title=f"{expiry.subject} {expiry_words(days)}",
                explanation="Its renewal has not run.",
                evidence=(("Expires", expiry_phrase(expiry.expires)),),
                primary=opens("Open certificate", expiry.url),
                remedies=(
                    FindingRemedy(
                        capability="certificate.renew",
                        target=node.label,
                        url=command_url("certificate.renew", node.label),
                    ),
                ),
            )
        )
    return tuple(findings)


# The rule this module raises: one family, whether HQ renews the certificate or
# where it is held does.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "certificate-expiring",
        # Its family holds certificates still to expire and those already past.
        "Certificate near or past expiry",
        Tone.ATTENTION,
        expiring,
        gathered=True,
        operator_action="Renew it from its page or where it is held, or fix its renewal.",
        no_help_reason="HQ cannot renew this certificate.",
        bars={
            Tone.SERIOUS: f"a certificate in use expires within {SERIOUS_DAYS}",
            Tone.ATTENTION: f"a certificate expires within {WARN_DAYS}",
        },
        clears=f"the certificate runs more than {WARN_DAYS} out",
    ),
)
