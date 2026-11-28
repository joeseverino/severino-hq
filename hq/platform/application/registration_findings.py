"""Findings about domain registrations that are about to lapse."""

from datetime import UTC

from .expiry import days_until
from .finding_model import Finding, FindingEstate, FindingRule, parse_stamp
from .finding_steps import Do
from .moments import expiry_words, when_day
from .references import Kind
from .thresholds import Threshold
from .tone import Tone
from .ui import ended

# Ninety days, the window a certificate gets: long enough to act on a domain
# whose renewal failed, short enough not to live in the queue.
WINDOW_DAYS = Threshold(90, "days")
SERIOUS_DAYS = Threshold(30, "days")


def _registration_lapsing(estate: FindingEstate) -> tuple[Finding, ...]:
    """A domain that runs out and will not renew itself.

    The one fact about a domain no other credential here can see, and the only
    one that takes everything else with it: the certificate, the records and
    every name inside the zone stop with the registration, while Cloudflare
    serves that zone perfectly for a domain about to stop being yours.
    """

    found: list[Finding] = []
    for node in estate.nodes():
        facts = dict(node.facts)
        expires = parse_stamp(facts.get("expires_at", ""))
        # Both halves are the rule. An expiry alone fires on every domain every
        # year and is a calendar; an expiry with auto-renew off is an outage
        # with a countdown. Only the registrar knows the second, which is why
        # the sweep reads it rather than RDAP, public and free but able to
        # answer only the half that means nothing on its own.
        if expires is None or facts.get("auto_renew") != "no":
            continue
        # A registrar reports a date, and a date parses naive. Compared against
        # an aware now that raises rather than answering, so the assumption is
        # made explicit here: a renewal date is a UTC day.
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        days = days_until(expires, estate.now)
        if days > WINDOW_DAYS.value:
            continue
        domain = facts.get("domain", node.label)
        registrar = facts.get("registrar", "")
        found.append(
            Finding(
                rule="registration-lapsing",
                # About the domain, so the domain's own page draws it.
                subject=Kind.ZONE.of(domain),
                title=f"{domain} {expiry_words(days)} and will not renew",
                severity=Tone.SERIOUS if days <= SERIOUS_DAYS.value else Tone.ATTENTION,
                explanation="Every name under it stops when it lapses.",
                evidence=(
                    ("Expires", when_day(expires)),
                    ("Auto-renew", "Off"),
                    ("Registrar", registrar or "Unknown"),
                ),
                fixes=(Do(ended(f"Renew {domain} or turn on auto-renew at {registrar or 'its registrar'}")),),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "registration-lapsing",
        "Domain registration expiring",
        Tone.SERIOUS,
        _registration_lapsing,
        operator_action="Renew the domain or turn on auto-renew at its registrar.",
        no_help_reason="HQ cannot renew a domain.",
        bars={
            Tone.SERIOUS: f"a domain that will not renew lapses within {SERIOUS_DAYS}",
            Tone.ATTENTION: f"a domain that will not renew lapses within {WINDOW_DAYS}",
        },
        clears=f"auto-renew is on, or the domain runs more than {WINDOW_DAYS} out",
    ),
)
