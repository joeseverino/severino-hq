"""Certificates a reading reports, near or past expiry.

Every reading that supplies the certificate facet and states an expiry counts,
whichever provider holds the certificate, and so does the certificate a proxy
reports serving a name with: one loaded from a file on the proxy's own host is
read nowhere else, and HQ neither issued nor manages it. The fact sits on the
connection that read it, because that is where the certificate lives; the
names it serves are evidence. Days left come from ``application.expiry``, the one rule every page
uses.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import replace
from typing import Any

from control_plane.observations import OBSERVATIONS
from control_plane.providers import PROVIDERS

from .expiry import days_until
from .facts import Joined, inventory_records
from .timestamps import moment
from .finding_model import FindingRule, built_findings

CERTIFICATE_EXPIRES = "certificate-expires"
# A provider that renews on its own does so with thirty days left; a
# certificate still inside three weeks of expiry missed that renewal.
WARN_DAYS = 21
SERIOUS_DAYS = 7


def add(
    nodes,
    readers: Callable[[Joined], Iterable[str]],
    holders: Callable[[Any, Mapping[str, Any]], Iterable[str]],
) -> None:
    """One fact per certificate that states an expiry, on each node that read it.

    ``readers`` names the nodes behind a certificate reading; ``holders`` the
    connection nodes a proxy's own record was read through.
    """

    for fact, node_ids in (*_read(readers), *_served(holders)):
        _kind, name, expires, _names = _parts(fact[1])
        for node_id in node_ids:
            node = nodes.get(node_id)
            # A proxy that also lists its certificates has already said this one.
            if node is not None and not _stated(node, name, expires):
                nodes[node_id] = replace(node, facts=node.facts + (fact,))


Found = Iterator[tuple[tuple[str, str], tuple[str, ...]]]


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
                controller_id=str(getattr(snapshot, "controller_id", "") or ""),
            )
            names = ",".join(spec.hostnames(record))
            yield (
                (CERTIFICATE_EXPIRES, "|".join((kind, spec.title(record), expires, names))),
                tuple(readers(joined)),
            )


def _served(holders) -> Found:
    """Each certificate a proxy serves with, every name it serves with it, and
    the nodes the proxy was read through."""

    found: dict[tuple[str, str, str], tuple[set[str], tuple[str, ...]]] = {}
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
                found.setdefault((node_id, *key), (set(), key))[0].update(served.hostnames)
    for (node_id, *_), (names, key) in found.items():
        yield (CERTIFICATE_EXPIRES, "|".join((*key, ",".join(sorted(names))))), (node_id,)


def _parts(value: str) -> list[str]:
    """A fact's kind, certificate name, expiry and the names it serves."""

    return (value.split("|") + [""] * 4)[:4]


def _stated(node, name: str, expires: str) -> bool:
    """Whether ``node`` already carries this certificate, by name and expiry."""

    for key, value in node.facts:
        if key != CERTIFICATE_EXPIRES:
            continue
        _kind, title, stamp, _names = _parts(value)
        if title == name and moment(stamp) == moment(expires):
            return True
    return False


def expiring(estate: Any) -> tuple[dict[str, Any], ...]:
    """A certificate a connection reads, inside three weeks of expiry or past it."""

    from control_plane.provider_spec import expiry_phrase

    found = []
    for node in estate.nodes():
        for key, value in node.facts:
            if key != CERTIFICATE_EXPIRES:
                continue
            kind, title, stamp, names = _parts(value)
            when = moment(stamp)
            if when is None:
                continue
            days = days_until(when, estate.now)
            if days > WARN_DAYS:
                continue
            spec = OBSERVATIONS.get(kind)
            # A served certificate is known only as what a proxy answers with.
            label = spec.label if spec is not None else "Certificate"
            found.append(
                dict(
                    rule="certificate-expiring",
                    subject=node.id,
                    title=(
                        f"{label} {title} has expired"
                        if days < 0
                        else f"{label} {title} expires {expiry_phrase(stamp)}"
                    ),
                    # One that serves no name breaks nothing when it lapses: it
                    # is left over, and the advice is to remove it, not renew it.
                    severity="serious" if names and days <= SERIOUS_DAYS else "attention",
                    explanation=(
                        "Clients get a certificate error on every name it serves "
                        "once it expires. Renew it where it is held, or find why "
                        "the automatic renewal failed."
                        if names
                        else "It serves no name, so nothing breaks when it expires. "
                        "Delete it where it is held rather than renew it."
                    ),
                    evidence=(
                        ("Certificate", title),
                        ("Held in", node.label),
                        ("Expires", expiry_phrase(stamp)),
                        *((("Serves", names.replace(",", ", ")),) if names else ()),
                    ),
                )
            )
    return tuple(sorted(found, key=lambda item: item["title"]))


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "certificate-expiring",
        "Certificate expiring",
        "attention",
        lambda estate: built_findings(expiring(estate)),
        operator_action=(
            "Renew the certificate where it is held, or fix the automatic renewal that should have renewed it, then request a fresh sweep."
        ),
        no_help_reason=(
            "The certificate is held by a provider HQ only reads, so it is renewed there."
        ),
    ),
)
