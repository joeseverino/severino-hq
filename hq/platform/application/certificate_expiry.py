"""Certificates a reading reports, near or past expiry.

Every reading with the certificate facet that states an expiry counts, whichever
provider holds it, and so does a certificate a proxy serves a name with, which
may be loaded from a file on the proxy's host and read nowhere else. The fact
sits on the connection that read it, where the certificate lives; the names it
serves are evidence. Days left come from ``application.expiry``.
"""

from collections.abc import Callable, Hashable, Iterable, Iterator, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from hq.domains.control_plane.certificate_authorities import UPLOADED
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.providers import PROVIDERS
lazy from hq.domains.control_plane.provider_spec import expiry_phrase

from .action_links import command_url
from .expiry import days_until
from .facts import Joined, inventory_records
from .finding_model import THEN_CHECK_AGAIN, Finding, FindingEstate, FindingRemedy, FindingRule, fact_values, opens
from .labels import lower_first
from .moments import expiry_words
from .timestamps import moment
from .ui import ended
lazy from .estate import managed_certificates

CERTIFICATE_EXPIRES = "certificate-expires"
# A certificate HQ renews, on its declaration: its name, expiry, renewal window and page.
RENEWAL = "certificate-renewal"
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
        _kind, name, expires, _names, _issuer = _parts(fact[1])
        for node_id in node_ids:
            node = nodes.get(node_id)
            # A proxy that also lists its certificates has already said this one.
            if node is not None and not _stated(node, name, expires):
                nodes[node_id] = replace(node, facts=(*node.facts, fact))


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
                controller_id=snapshot.controller_id,
            )
            names = ",".join(spec.hostnames(record))
            fact = "|".join((kind, spec.title(record), expires, names, spec.issuer(record)))
            yield (
                (CERTIFICATE_EXPIRES, fact),
                tuple(readers(joined)),
            )


def _served(holders) -> Found:
    """Each certificate a proxy serves with, every name it serves with it, and
    the nodes the proxy was read through. HQ neither issued nor manages one
    loaded from a file on the proxy's host, so no other reading lists it."""

    found: dict[tuple[str, ...], tuple[set[str], tuple[str, ...], str]] = {}
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
        yield (CERTIFICATE_EXPIRES, "|".join((*key, ",".join(sorted(names)), issuer))), (node_id,)


def _parts(value: str) -> list[str]:
    """A fact's kind, certificate name, expiry, the names it serves and its issuer."""

    return (value.split("|") + [""] * 5)[:5]


def _stated(node, name: str, expires: str) -> bool:
    """Whether ``node`` already carries this certificate, by name and expiry."""

    for key, value in node.facts:
        if key != CERTIFICATE_EXPIRES:
            continue
        _kind, title, stamp, _names, _issuer = _parts(value)
        if title == name and moment(stamp) == moment(expires):
            return True
    return False


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

    held = serving(
        (_served_as(node, value), when, (node, value, days_until(when, estate.now)))
        for node in estate.nodes()
        for value, when in _expiries(node)
    )
    alike: dict[tuple[str, str, str], list[tuple[Any, str, int]]] = {}
    for node, value, days in held:
        if days <= WARN_DAYS:
            alike.setdefault(_reads_as(value, days), []).append((node, value, days))
    found = []
    for group in alike.values():
        said = _telling(group) if len(group) > 1 else None
        found += [
            _finding(node, value, days, apart=said(node, value) if said else ("", "")) for node, value, days in group
        ]
    return tuple(sorted(found, key=lambda item: item.title)) + renewal_due(estate)


def _expiries(node) -> Iterator[tuple[str, datetime]]:
    """Each certificate fact on ``node`` with a readable expiry, and that expiry."""

    for key, value in node.facts:
        if key == CERTIFICATE_EXPIRES and (when := moment(_parts(value)[2])) is not None:
            yield value, when


def _served_as(node, value: str) -> tuple[str, ...]:
    """The name a certificate fact on ``node`` is judged under: its kind and
    the first name it serves, as the estate's edge certificates are. One that
    serves no name stands for none, so it is judged alone."""

    kind, _title, _stamp, names, _issuer = _parts(value)
    return (node.id, kind, names.split(",")[0]) if names else (node.id, value)


def _label(kind: str) -> str:
    spec = OBSERVATIONS.get(kind)
    # A served certificate is known only as what a proxy answers with.
    return spec.label if spec is not None else "Certificate"


def _reads_as(value: str, days: int) -> tuple[str, str, str]:
    """What a finding's title says of a certificate before it is told apart."""

    kind, title, *_rest = _parts(value)
    return _label(kind), title, expiry_words(days)


def _issued(_node, value: str) -> tuple[str, str]:
    issuer = _parts(value)[4]
    # An uploaded one has no issuer to name, so the word leads the title.
    return (UPLOADED.capitalize(), "") if issuer == UPLOADED else ("", f"from {issuer or 'an unnamed issuer'}")


# What tells two certificates of one title apart, in the order tried: two for
# one name (a zone's edge certificate and its backup) by who issued them or
# what they serve, and one held in two places by where. Each says the words
# that lead the title and those that follow the certificate's name.
Telling = Callable[[Any, str], tuple[str, str]]
_TELLINGS: tuple[Telling, ...] = (
    _issued,
    lambda _node, value: ("", f"for {_parts(value)[3].replace(',', ', ') or 'no name'}"),
    lambda node, _value: ("", f"in {node.label}"),
)


def _telling(group: list[tuple[Any, str, int]]) -> Telling:
    """The first telling that says each of ``group`` differently, else the last."""

    for telling in _TELLINGS:
        if len({telling(node, value) for node, value, _days in group}) == len(group):
            return telling
    return _TELLINGS[-1]


def _finding(node, value: str, days: int, apart: tuple[str, str] = ("", "")) -> Finding:
    """The finding for one certificate fact with ``days`` left, ``apart``
    saying what tells it from another whose title would read the same."""

    kind, title, stamp, names, issuer = _parts(value)
    label, _title, said = _reads_as(value, days)
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
        severity="serious" if names and days <= SERIOUS_DAYS else "attention",
        explanation=_SERVING_EXPLANATION if names else _UNUSED_EXPLANATION,
        evidence=(
            ("Certificate", title),
            ("Held in", node.label),
            ("Expires", expiry_phrase(stamp)),
            *((("Issued by", issuer),) if issuer else ()),
            *((("Serves", names.replace(",", ", ")),) if names else ()),
        ),
    )


_SERVING_EXPLANATION = (
    "Visitors get a certificate error on every name it serves once it expires. "
    "Renew it where it is held, or find why the automatic renewal failed."
)
_UNUSED_EXPLANATION = "It serves no name, so nothing breaks when it expires. Delete it where it is held."


def add_renewals(nodes) -> None:
    """Each certificate HQ renews, as a fact on its declaration's node."""

    for expiry in managed_certificates():
        node = nodes.get(f"resource:{expiry.resource_key}")
        if node is not None:
            said = (expiry.subject, expiry.expires.isoformat(), str(expiry.renewal_window_days), expiry.url)
            fact = (RENEWAL, "|".join(said))
            nodes[node.id] = replace(node, facts=(*node.facts, fact))


def renewal_due(estate: FindingEstate) -> tuple[Finding, ...]:
    """A certificate HQ renews, inside its renewal window: the renewal has not run."""

    found = []
    for node in estate.nodes():
        for value in fact_values(node, RENEWAL):
            name, stamp, window, url = value.rsplit("|", 3)
            if (when := moment(stamp)) is None or (days := days_until(when, estate.now)) > int(window):
                continue
            found.append(
                Finding(
                    rule="certificate-expiring",
                    subject=node.id,
                    severity="serious" if days <= SERIOUS_DAYS else "attention",
                    title=f"{name} {expiry_words(days)}",
                    explanation=f"Expires {ended(expiry_phrase(stamp))} Renewal has not run; renew it.",
                    primary=opens("Open certificate", url),
                    remedies=(
                        FindingRemedy(
                            capability="certificate.renew",
                            target=node.label,
                            url=command_url("certificate.renew", node.label),
                        ),
                    ),
                )
            )
    return tuple(found)


# The rule this module raises: one family, whether HQ renews the certificate or
# where it is held does.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "certificate-expiring",
        # Its family holds certificates still to expire and those already past.
        "Certificate near or past expiry",
        "attention",
        expiring,
        operator_action=(
            "Renew the certificate from its page in HQ, or where it is held, or fix its automatic renewal. "
            f"{THEN_CHECK_AGAIN}"
        ),
        no_help_reason="HQ cannot renew this certificate.",
    ),
)
