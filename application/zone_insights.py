"""What is worth knowing about a domain, contributed one fact at a time.

The zone page began as four cards restating DNS records back at the operator --
its MX hosts, its SPF string, its DMARC policy, its CAA entries. All true, all
available in Cloudflare's own dashboard, and none of them a reason to have
built this.

What HQ can say that Cloudflare cannot is how a domain relates to everything
*else* HQ holds: which services answer inside it, which managed certificate
covers it and when that expires, and -- the one that actually bites -- whether
the domain's own CAA record permits the authority HQ renews with. A zone that
forbids Let's Encrypt while HQ renews a Let's Encrypt certificate for it is a
failure scheduled for the day the certificate expires, and nothing else in the
system is in a position to notice.

Each function here takes a Zone and returns one insight, or None when it has
nothing to say. They are registered by name in ``zones.ZONE_INSIGHTS`` so a new
one is an entry rather than an edit to a page.
"""

from __future__ import annotations

import re
from django.urls import reverse

from control_plane.models import ManagedResource
from control_plane.providers import (
    CERTIFICATE_KIND,
    PROVIDERS,
    UPLOADED_CERTIFICATE_KIND,
    caa_parts,
    expiry_phrase,
)

from .infrastructure import delivery_targets, resolved_spec
from .known_hosts import operator, registrable

from .ui import ListRow
from .zones import ZoneInsight

# The authority HQ's own certificate provider issues from. Stated here because
# the insight below compares it against what a zone's CAA record permits, and
# "Let's Encrypt" appears in that provider's summary as prose rather than as
# something a comparison can read.
MANAGED_ISSUER = "letsencrypt.org"
ISSUING_PROVIDER = CERTIFICATE_KIND


def _in_zone(name: str, zone: str) -> bool:
    candidate = name.lower().rstrip(".").removeprefix("*.")
    return candidate == zone or candidate.endswith(f".{zone}")


def services(zone) -> ZoneInsight | None:
    """How much of this domain HQ actually runs.

    Counted from the service catalogue rather than from the records here,
    because a name is a service when something is expected to answer for it,
    and that is a judgement the service view already makes.
    """

    from .services import service_catalog

    inside = [s for s in service_catalog() if _in_zone(s.hostname, zone.zone)]
    if not inside:
        return ZoneInsight(
            label="Services",
            value="None",
            detail="Nothing in this domain has anything declared behind it.",
        )
    unhealthy = [s for s in inside if s.faults]
    # A count, with the list one click beneath it. A busy domain has more
    # services than a card can name, and naming the first of thirty is worse
    # than naming none -- but losing them to a bare number is worse still, so
    # the number opens onto the whole list.
    return ZoneInsight(
        label="Services",
        value=f"{len(inside)} service{'' if len(inside) == 1 else 's'}",
        detail=(
            f"{len(unhealthy)} missing something behind it."
            if unhealthy
            else "All fully wired."
        ),
        # A real page that does the same job, so the card survives the dialog
        # not opening.
        url=reverse("control_plane:services"),
        rows=tuple(
            ListRow(
                title=service.hostname,
                url=service.url,
                status=service.status,
                badge=service.status_label,
            )
            for service in inside
        ),
        concern=bool(unhealthy),
    )


def certificates(zone) -> ZoneInsight | None:
    """The managed certificates covering this domain, and whether it allows them.

    The second half is the point. A CAA record naming which authorities may
    issue for a domain is a security control, and it silently becomes an outage
    when it excludes the authority that renews the certificate already serving
    the domain. Nothing at Cloudflare knows which certificates HQ renews, and
    nothing in the certificate registry knows what the zone permits.
    """

    targets = delivery_targets()
    covering = []
    for resource in ManagedResource.objects.filter(
        kind__in=(CERTIFICATE_KIND, UPLOADED_CERTIFICATE_KIND), enabled=True
    ):
        provider = PROVIDERS.get(resource.kind)
        if provider is None or provider.hostnames is None:
            continue
        spec = resolved_spec(resource, targets)
        try:
            names = tuple(provider.hostnames(spec))
        except (KeyError, TypeError, ValueError):
            continue
        if any(_in_zone(name, zone.zone) for name in names):
            covering.append((resource, names))

    permitted = _caa_issuers(zone)
    if not covering:
        if permitted:
            return ZoneInsight(
                label="Certificates",
                value="None managed here",
                detail=(
                    "Issuance is restricted to "
                    + ", ".join(sorted(permitted))
                    + "."
                ),
            )
        return ZoneInsight(
            label="Certificates",
            value="None managed here",
            detail="No CAA record either, so any authority may issue for it.",
        )

    resource, names = covering[0]
    expires = expiry_phrase(str((resource.status or {}).get("not_after", "")))
    extra = f" and {len(covering) - 1} more" if len(covering) > 1 else ""

    # The cross-check. Only a certificate HQ issues has an authority HQ can
    # predict; an uploaded one was signed by something HQ never chose.
    renewed = [item for item, _ in covering if item.kind == ISSUING_PROVIDER]
    if renewed and permitted and MANAGED_ISSUER not in permitted:
        return ZoneInsight(
            label="Certificates",
            value=f"{resource.key}{extra}",
            detail=(
                f"This domain's CAA record permits only {', '.join(sorted(permitted))}, "
                f"so the next renewal of {renewed[0].key} will be refused. Add "
                f"{MANAGED_ISSUER} to the CAA records, or the certificate lapses."
            ),
            url=reverse("control_plane:detail", kwargs={"key": renewed[0].key}),
            concern=True,
        )

    # Just the expiry. The card previously also listed which names the
    # certificate covered and confirmed that CAA permitted the issuer -- four
    # lines of prose to say "it is fine", which is what a card should say by
    # being short. What it covers is on the certificate's own page, and the CAA
    # check has a card of its own the moment it has something to report.
    return ZoneInsight(
        label="Certificates",
        value=f"{resource.key}{extra}",
        detail=f"Expires {expires}." if expires else "",
        url=reverse("control_plane:detail", kwargs={"key": resource.key}),
    )


def email(zone) -> ZoneInsight | None:
    """Whether this domain can receive mail, and whether anyone can forge it.

    One card rather than three. MX, SPF and DMARC are not three facts, they are
    one answer given in three records, and split across three cards the reader
    has to assemble it themselves.
    """

    mail = sorted(
        (r for r in zone.records if r.record_type == "MX"),
        key=lambda r: (r.priority if r.priority is not None else 0),
    )
    spf = [
        r for r in zone.records
        if r.record_type == "TXT" and "v=spf1" in r.content.lower()
    ]
    dmarc = [
        r for r in zone.records
        if r.record_type == "TXT" and r.name.startswith("_dmarc.")
    ]

    if not (mail or spf or dmarc):
        return ZoneInsight(
            label="Email",
            url=reverse("zones:mail", kwargs={"zone": zone.zone}),
            value="Not configured",
            detail=(
                "No MX, SPF or DMARC record. This domain receives no mail, and "
                "nothing stops anyone sending mail that claims to come from it."
            ),
        )

    # Assembled as whole sentences rather than joined fragments. Built by
    # capitalising a comma-joined list, this read "Spf, dmarc rejects
    # forgeries." -- which lowercases two acronyms and states nothing clearly.
    sentences = []
    if not mail:
        sentences.append("Nothing accepts mail for this domain.")
    sentences.append("SPF is published." if spf else "No SPF record.")
    sentences.append(
        f"{_dmarc_policy(dmarc[0].content)}." if dmarc else "No DMARC record."
    )
    return ZoneInsight(
        label="Email",
        url=reverse("zones:mail", kwargs={"zone": zone.zone}),
        value=_mail_host(mail) if mail else "Not received",
        detail=" ".join(sentences),
    )


def leftover_challenges(zone) -> ZoneInsight | None:
    """Challenge records that outlived the issuance they existed for.

    The one judgement this page makes without a declared policy, because it
    does not need one: a challenge record exists for the seconds an authority
    takes to verify a request and is removed afterwards. One still present at a
    scheduled sweep was left behind and serves nothing.
    """

    stale = [
        record for record in zone.records
        if record.record_type == "TXT" and record.name.startswith("_acme-challenge.")
    ]
    if not stale:
        return None
    return ZoneInsight(
        label="Left-over ACME challenges",
        value=f"{len(stale)} left behind",
        detail=(
            "These are created while a certificate is being issued and removed "
            "once it is. Ones still here were not cleaned up, and serve no "
            "purpose."
        ),
        concern=True,
    )


def _mail_host(mail) -> str:
    """Who actually receives mail for this domain, read off the MX records.

    "2 mail servers" was a true and useless answer: the count of MX records is
    a redundancy detail, and the question is who has the mailbox.
    """

    hosts = {registrable(record.content) for record in mail}
    if len(hosts) != 1:
        # Split across operators, unusual enough to state plainly rather than
        # summarise into one name that would be half wrong.
        return f"{len(mail)} mail servers"
    return operator(hosts.pop())


def _caa_issuers(zone) -> set[str]:
    """Every authority this domain's CAA records permit to issue.

    ``iodef`` is deliberately excluded: it names where to report a violation,
    not who may issue, and counting it as an issuer would make a domain look
    restricted to an email address -- and a certificate HQ renews look doomed
    when it is fine.
    """

    issuers: set[str] = set()
    for record in zone.records:
        if record.record_type != "CAA":
            continue
        parts = caa_parts(record.content)
        if parts is None:
            continue
        _, tag, value = parts
        if tag in {"issue", "issuewild"} and value:
            issuers.add(value.split(";")[0].strip().lower())
    return issuers


def _dmarc_policy(content: str) -> str:
    match = re.search(r"\bp=([a-z]+)", content, re.IGNORECASE)
    if not match:
        return "DMARC published"
    return {
        "reject": "DMARC rejects forgeries",
        "quarantine": "DMARC quarantines forgeries",
        "none": "DMARC is monitoring only",
    }.get(match.group(1).lower(), f"DMARC p={match.group(1).lower()}")


# How Cloudflare's own words for a TLS mode read to somebody who did not set it.
# "flexible" means the browser gets TLS and the origin gets plain HTTP, which is
# worth saying in those terms rather than repeating the label.
_TLS_MODE = {
    "off": ("Off", "Served over plain HTTP."),
    "flexible": (
        "Flexible",
        "Encrypted to Cloudflare and plain HTTP onward to the origin.",
    ),
    "full": ("Full", "Encrypted to the origin, whose certificate is not checked."),
    "strict": ("Full (strict)", "Encrypted to the origin and its certificate checked."),
    "full_strict": (
        "Full (strict)",
        "Encrypted to the origin and its certificate checked.",
    ),
}


def posture(zone) -> ZoneInsight | None:
    """How this domain answers over TLS, as Cloudflare currently holds it.

    Stated, never flagged. HQ can read this now -- `cloudflare_api` carries the
    account surface and the sweep collects it -- but it holds no declared
    posture to compare against, and a control plane that reports drift from a
    policy nobody wrote is inventing one. The two things here that are wrong by
    their own definition already have their own insights.

    Absent rather than empty when the account credential could not answer. A
    domain whose posture HQ could not read is not a domain served over plain
    HTTP, and a card saying "Off" because a token lacked a permission is worse
    than no card.
    """

    from control_plane.models import ProviderInventory

    wanted = zone.zone.strip().lower().rstrip(".")
    found: dict[str, str] = {}
    for snapshot in ProviderInventory.objects.filter(kind="cloudflare.zone"):
        for record in snapshot.records:
            if str(record.get("zone", "")).strip().lower().rstrip(".") != wanted:
                continue
            found = dict(record.get("posture") or {})
    if not found:
        return None

    mode = str(found.get("ssl", "")).lower()
    label, explanation = _TLS_MODE.get(mode, (mode.replace("_", " ").title(), ""))
    minimum = str(found.get("min_tls_version", "")).strip()
    detail = [explanation] if explanation else []
    if minimum:
        detail.append(f"Nothing below TLS {minimum} is accepted.")
    if found.get("always_use_https") == "on":
        detail.append("HTTP is redirected to HTTPS.")
    return ZoneInsight(
        label="TLS posture",
        value=label or "Unknown",
        detail=" ".join(detail),
    )
