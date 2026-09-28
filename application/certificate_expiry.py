"""Certificates a reading reports, near or past expiry.

Every reading that supplies the certificate facet and states an expiry counts,
whichever provider holds the certificate. The fact sits on the connection that
read it, because that is where the certificate lives; the names it serves are
evidence. Days left come from ``application.expiry``, the one rule every page
uses.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import Any

from control_plane.observations import OBSERVATIONS

from .expiry import days_until
from .facts import Joined, inventory_records
from .ui import moment

CERTIFICATE_EXPIRES = "certificate-expires"
# A provider that renews on its own does so with thirty days left; a
# certificate still inside three weeks of expiry missed that renewal.
WARN_DAYS = 21
SERIOUS_DAYS = 7


def add(nodes, readers: Callable[[Joined], Iterable[str]]) -> None:
    """One fact per expiring certificate reading, on each node that read it."""

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
            fact = (CERTIFICATE_EXPIRES, "|".join((kind, spec.title(record), expires, names)))
            for node_id in readers(joined):
                node = nodes.get(node_id)
                if node is not None and fact not in node.facts:
                    nodes[node_id] = replace(node, facts=node.facts + (fact,))


def expiring(estate: Any) -> tuple[dict[str, Any], ...]:
    """A certificate a connection reads, inside three weeks of expiry or past it."""

    from control_plane.provider_spec import expiry_phrase

    found = []
    for node in estate.nodes():
        for key, value in node.facts:
            if key != CERTIFICATE_EXPIRES:
                continue
            kind, title, stamp, names = (value.split("|") + [""] * 4)[:4]
            when = moment(stamp)
            if when is None:
                continue
            days = days_until(when, estate.now)
            if days > WARN_DAYS:
                continue
            spec = OBSERVATIONS.get(kind)
            label = spec.label if spec is not None else kind
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
