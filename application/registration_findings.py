"""Findings about domain registrations that are about to lapse."""

from __future__ import annotations

from datetime import timezone as dt_timezone

from .expiry import days_until
from .finding_model import Finding, FindingEstate
from .finding_model import parse_stamp
from .finding_model import FindingRule


def _registration_lapsing(estate: FindingEstate) -> tuple[Finding, ...]:
    """A domain that runs out and will not renew itself.

    The one fact about a domain no other credential here can see, and the only
    one that takes everything else with it. HQ renews the certificate,
    reconciles the records and serves every name inside the zone, and none of
    it survives the registration lapsing. Cloudflare will serve that zone
    perfectly for a domain about to stop being yours.

    Both halves are the rule. An expiry alone fires on every domain every year
    and is a calendar, not a finding; an expiry with auto-renew off is an outage
    with a countdown. The registrar knows the second, which is why the sweep
    reads the registrar rather than RDAP: RDAP is public and free and can only
    ever answer the half that means nothing on its own.

    Ninety days, matching the window a certificate gets: long enough to act on a
    domain whose renewal failed, short enough not to live in the queue.
    """

    found: list[Finding] = []
    for node in estate.nodes():
        facts = dict(node.facts)
        expires = parse_stamp(facts.get("expires_at", ""))
        if expires is None or facts.get("auto_renew") != "no":
            continue
        # A registrar reports a date, and a date parses naive. Compared against
        # an aware now that raises rather than answering, so the assumption is
        # made explicit here: a renewal date is a UTC day.
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=dt_timezone.utc)
        days = days_until(expires, estate.now)
        if days > 90:
            continue
        domain = facts.get("domain", node.label)
        found.append(
            Finding(
                rule="registration-lapsing",
                subject=node.id,
                title=f"{domain} expires in {days} days and will not renew",
                severity="serious" if days <= 30 else "attention",
                explanation=(
                    f"On {expires.date().isoformat()} its "
                    "records, certificate and every name under it stop working. "
                    "Renew it or turn on auto-renew at the registrar."
                ),
                evidence=(
                    ("Expires", expires.date().isoformat()),
                    ("Auto-renew", "off"),
                    ("Registrar", facts.get("registrar", "unknown")),
                ),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "registration-lapsing",
        "Domain registration expiring",
        "serious",
        _registration_lapsing,
        operator_action=(
            "Renew the domain or turn on auto-renew at its registrar."
        ),
    ),
)
