"""The service page's band for who Nginx Proxy Manager lets in.

Read from the access list reading joined to the service's names, so the band
and the relation graph say the same thing.
"""

from __future__ import annotations

from typing import Any

from hq.domains.control_plane.observations.npm import ACCESS_LIST_KIND

from .facts import Subject, readings
from .service_context import Cell, ServiceSection
from .ui import counted


def _satisfy(record: Any) -> str:
    """How a client gets in: every rule, or any one of them."""

    if not record.get("logins"):
        return "Address rules"
    return "Address or login" if record.get("satisfy_any") else "Address and login"


def access(service, project) -> ServiceSection | None:
    """Each access list guarding one of the service's names, and whom it admits."""

    del project
    names = (getattr(service, "hostname", ""), *(getattr(service, "aliases", ()) or ()))
    subject = Subject.of(hostnames=names)
    if not subject:
        return None
    rows = []
    for joined in readings().about(subject, kinds=(ACCESS_LIST_KIND,)):
        record = joined.record
        rules = [
            f"{rule.get('directive', '')} {rule.get('address', '')}"
            for rule in record.get("clients") or ()
        ]
        logins = record.get("logins") or ()
        rows.append(
            (
                Cell(joined.title or str(record.get("id", ""))),
                Cell(_satisfy(record)),
                Cell("; ".join(rules)) if rules else Cell("any address", muted=True),
                Cell(f"{counted(len(logins), 'login')}: {', '.join(logins)}")
                if logins
                else Cell("none", muted=True),
            )
        )
    if not rows:
        return None
    return ServiceSection(
        id="access",
        label="Who is allowed",
        columns=("Access list", "Admits by", "Addresses", "Logins"),
        records=tuple(rows),
    )
