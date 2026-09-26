"""What is worth knowing about a domain, contributed one fact at a time.

The zone page began as four cards restating DNS records back at the operator,
its MX hosts, its SPF string, its DMARC policy, its CAA entries. All true, all
available in Cloudflare's own dashboard, and none of them a reason to have
built this.

What HQ can say that Cloudflare cannot is how a domain relates to everything
*else* HQ holds: which services answer inside it, which managed certificate
covers it and when that expires, and (the one that actually bites) whether
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

from control_plane.credential_reads import REGISTRAR_READ
from control_plane.models import ManagedResource
from control_plane.provider_adapters.contracts import PERMISSION_REFUSAL, cloudflare_refusal
from control_plane.providers import (
    CERTIFICATE_KIND,
    PROVIDERS,
    UPLOADED_CERTIFICATE_KIND,
    caa_parts,
    expiry_phrase,
)

from control_plane.names import in_zone, normalized_hostname

from .entity_links import entity_link
from .facts import (
    Subject,
    inventory_about,
    inventory_records,
    readings as stored_readings,
)
from .infrastructure import delivery_targets, resolved_spec
from .known_hosts import operator, registrable


from .ui import ListRow, counted, ended
from .zones import ZONE_KIND, ZoneInsight

# The authority HQ's own certificate provider issues from. Stated here because
# the insight below compares it against what a zone's CAA record permits, and
# "Let's Encrypt" appears in that provider's summary as prose rather than as
# something a comparison can read.
MANAGED_ISSUER = "letsencrypt.org"
ISSUING_PROVIDER = CERTIFICATE_KIND


def _earliest(stamps) -> str:
    """The earliest of several ISO 8601 stamps, or ""."""

    from .ui import moment

    found = [(when, stamp) for stamp in stamps if (when := moment(stamp)) is not None]
    return min(found)[1] if found else ""


def services(zone) -> ZoneInsight | None:
    """What runs in this domain: its services, what serves them, what fronts them.

    Counted from the service catalogue, which decides what a service is and
    when one is parked. Readings joined to names in the domain add what serves
    them (a Pages project) and how many sit behind an overlay (Access).
    """

    from .services import services_by_zone
    from .zones import zone_names

    members = services_by_zone(zone_names()).get(zone.zone, ())
    inside = [member.service for member in members if member.service is not None]
    joined = stored_readings().about(Subject.of(zones=(zone.zone,)))
    serving = tuple(
        dict.fromkeys(
            entity_link(item.kind, "", record=item.record)
            for item in joined
            if item.facet in ("runtime", "network") and item.title
        )
    )
    overlays = _overlays(joined)
    if not members:
        return ZoneInsight(
            label="Services",
            value="None",
            detail="Nothing in this domain has anything declared behind it.",
            links=(("Served by", serving),) if serving else (),
            note=overlays,
        )
    parked = [service for service in inside if service.origin and service.origin.parked]
    unhealthy = [service for service in inside if service.faults]
    if len(parked) == len(members):
        handlers = sorted({service.provider_answers for service in parked} - {""})
        value = "Parked"
        detail = (
            f"Handled at {', '.join(handlers)}." if handlers else "Nothing answers there."
        )
    else:
        value = counted(len(members), "service")
        detail = (
            f"{len(unhealthy)} missing something behind it."
            if unhealthy
            else "All fully wired."
        )
    # A count, with the list one click beneath it: the number opens onto the
    # whole list, and ``url`` reaches a page that does the same job.
    return ZoneInsight(
        label="Services",
        value=value,
        detail=detail,
        url=reverse("control_plane:services"),
        rows=tuple(
            ListRow(
                title=member.hostname,
                url=member.url,
                status=member.status,
                badge=member.status_label,
            )
            for member in members
        ),
        links=(("Served by", serving),) if serving else (),
        note=overlays,
        concern=bool(unhealthy) and len(parked) != len(members),
    )


def _overlays(joined) -> str:
    """"2 behind Access": readings that supply no facet, counted by relation."""

    counts: dict[str, set] = {}
    for item in joined:
        if item.facet or not item.spec.joins_hostnames:
            continue
        counts.setdefault(item.relation, set()).update(item.hostnames)
    return " · ".join(
        f"{len(names)} {relation[:1].lower()}{relation[1:]}"
        for relation, names in sorted(counts.items())
    )


def certificates(zone) -> ZoneInsight | None:
    """The managed certificates covering this domain, and whether it allows them.

    The second half is the point. A CAA record naming which authorities may
    issue for a domain is a security control, and it silently becomes an outage
    when it excludes the authority that renews the certificate already serving
    the domain. Nothing at Cloudflare knows which certificates HQ renews, and
    nothing in the certificate registry knows what the zone permits.
    """

    covering = _covering(zone)
    permitted = _caa_issuers(zone)
    index = stored_readings()
    edge = index.about(Subject.of(zones=(zone.zone,)), facets=("certificate",))
    if not covering:
        if edge:
            return _edge_certificates(edge)
        refused = index.unread(facets=("certificate",))
        if refused:
            return ZoneInsight(
                label="Certificates",
                value="None managed here",
                detail="Edge certificates are not readable.",
                note=refused[0].detail,
            )
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
            url=entity_link("resource", renewed[0].key).url,
            concern=True,
        )

    # Just the expiry. What it covers is on the certificate's own page, and the
    # CAA check has a card of its own when it has something to report.
    return ZoneInsight(
        label="Certificates",
        value=f"{resource.key}{extra}",
        detail=f"Expires {expires}." if expires else "",
        url=entity_link("resource", resource.key).url,
        note=_edge_certificates(edge).detail if edge else "",
    )


def _covering(zone) -> list:
    """``(resource, names)`` for each managed certificate naming a name in the zone."""

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
        if any(in_zone(name, zone.zone) for name in names):
            covering.append((resource, names))
    return covering


def security(zone) -> ZoneInsight | None:
    """How this domain answers over TLS, and with which certificates.

    One card: the TLS mode, the edge and managed certificates and the earliest
    expiry among them. A concern when a CAA record refuses the authority HQ
    renews with, or when another domain here is held to a stronger posture.
    """

    tls = posture(zone)
    certificates_card = certificates(zone)
    edge = stored_readings().about(Subject.of(zones=(zone.zone,)), facets=("certificate",))
    covering = _covering(zone)
    earliest = _earliest(
        [item.expires for item in edge]
        + [str((resource.status or {}).get("not_after", "")) for resource, _ in covering]
    )
    parts = [tls.value if tls else "TLS not read"]
    if edge:
        parts.append(f"{len(edge)} edge")
    if covering:
        parts.append(f"{len(covering)} managed")
    if earliest:
        parts.append(f"earliest {expiry_phrase(earliest)}")
    stronger = _stronger_elsewhere(zone)
    detail = [tls.detail] if tls and tls.detail else []
    if certificates_card is not None and certificates_card.concern:
        detail.append(certificates_card.detail)
    if stronger:
        detail.append(ended(f"Stronger on {stronger}"))
    issuers = sorted({item.issuer for item in edge if item.issuer})
    return ZoneInsight(
        label="Security",
        value=" · ".join(parts),
        detail=" ".join(detail),
        url=certificates_card.url if certificates_card is not None else "",
        note=ended(f"Edge issued by {', '.join(issuers)}") if issuers else (
            certificates_card.note if certificates_card is not None else ""
        ),
        concern=bool(
            (tls and tls.concern)
            or (certificates_card is not None and certificates_card.concern)
            or stronger
        ),
    )


# Cloudflare's documented order of SSL modes, weakest first.
_SSL_ORDER = ("off", "flexible", "full", "strict")


def _strength(found: dict) -> tuple[int, tuple[int, ...]] | None:
    """A readable posture as (mode rank, minimum TLS version), or None."""

    if not found or found.get("unread"):
        return None
    mode = str(found.get("ssl", "")).lower().replace("full_strict", "strict")
    if mode not in _SSL_ORDER:
        return None
    version = tuple(
        int(part) for part in str(found.get("min_tls_version", "") or "0").split(".") if part.isdigit()
    )
    return _SSL_ORDER.index(mode), version


def _stronger_elsewhere(zone) -> str:
    """Other domains read here with a stronger TLS posture, as a phrase."""

    postures: dict[str, dict] = {}
    for _snapshot, record in inventory_records(ZONE_KIND):
        name = normalized_hostname(record.get("zone"))
        if name:
            postures[name] = dict(record.get("posture") or {})
    mine = _strength(postures.get(zone.zone, {}))
    if mine is None:
        return ""
    better = []
    for name, found in sorted(postures.items()):
        theirs = _strength(found)
        if name != zone.zone and theirs is not None and theirs > mine:
            label = _TLS_MODE.get(str(found.get("ssl", "")).lower(), ("", ""))[0]
            minimum = str(found.get("min_tls_version", "") or "")
            better.append(f"{name} ({label}{f', TLS {minimum}' if minimum else ''})")
    return ", ".join(better)


def _edge_certificates(edge) -> ZoneInsight:
    """Edge certificates covering the zone: how many, the earliest expiry, who issued them."""

    earliest = _earliest(item.expires for item in edge)
    issuers = sorted({item.issuer for item in edge if item.issuer})
    detail = []
    if earliest:
        detail.append(f"Earliest expires {expiry_phrase(earliest)}.")
    if issuers:
        detail.append(ended(f"Issued by {', '.join(issuers)}"))
    return ZoneInsight(
        label="Certificates",
        value=counted(len(edge), "edge certificate", "edge certificates"),
        detail=" ".join(detail),
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
            note="Next: publish v=spf1 -all and a DMARC p=reject record.",
        )

    # Assembled as whole sentences rather than joined fragments. Built by
    # capitalising a comma-joined list, this read "Spf, dmarc rejects
    # forgeries.", which lowercases two acronyms and states nothing clearly.
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
        notice=True,
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
    restricted to an email address, and a certificate HQ renews look doomed
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

    Stated, never flagged. HQ can read this now: `cloudflare_api` carries the
    account surface and the sweep collects it, but it holds no declared
    posture to compare against, and a control plane that reports drift from a
    policy nobody wrote is inventing one. The two things here that are wrong by
    their own definition already have their own insights.

    Absent rather than empty when the account credential could not answer. A
    domain whose posture HQ could not read is not a domain served over plain
    HTTP, and a card saying "Off" because a token lacked a permission is worse
    than no card.
    """

    found: dict[str, str] = {}
    for _snapshot, record in inventory_about(ZONE_KIND, Subject.of(hostnames=(zone.zone,))):
        found = dict(record.get("posture") or {})
    if not found:
        return None
    if found.get("unread"):
        return ZoneInsight(
            label="TLS posture",
            value="Not readable",
            detail=f"The Cloudflare account credential could not read it: {found['unread']}",
            concern=True,
        )

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


def registration(zone) -> ZoneInsight | None:
    """When this domain stops being yours, and whether it renews itself.

    The registrar knows both. Without registrar access the public registry
    still says when; whether it renews itself is then unknown, and said so.
    """

    from datetime import datetime, timezone

    subject = Subject.of(hostnames=(zone.zone,))
    found: dict[str, object] = {}
    for _snapshot, record in inventory_about(ZONE_KIND, subject):
        found = dict(record.get("registration") or {})
    refused = str(found.get("unread", "") or "")
    expires = "" if refused else str(found.get("expires_at", ""))
    if not expires:
        refusal = str(found.get("refusal", "") or "") or cloudflare_refusal(refused)
        return _public_registration(subject, refused, refusal)
    try:
        when = datetime.fromisoformat(expires).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    renews = bool(found.get("auto_renew"))
    days = (when - datetime.now(timezone.utc)).days
    return ZoneInsight(
        label="Registration",
        value=expiry_phrase(when.isoformat()),
        detail=(
            "Renews itself at the registrar."
            if renews
            else "Auto-renew is off, so this has to be renewed by hand."
        ),
        # Only when both halves are true. A date alone is a calendar entry.
        concern=days <= 90 and not renews,
    )


def _registrar_note(refused: str, refusal: str) -> tuple[str, str]:
    """``(note, title)`` for a registrar read that was refused.

    A missing permission is said as the permission to add, with the
    provider's words kept in the title. Anything else, a refused credential included, is
    said in the provider's words.
    """

    if not refused:
        return "", ""
    if refusal == PERMISSION_REFUSAL:
        return f"Add {REGISTRAR_READ} to see auto-renew.", refused
    return f"Registrar not read: {refused}", ""


def _public_registration(subject, refused: str, refusal: str = "") -> ZoneInsight | None:
    """The public registry's expiry, when the registrar's could not be read."""

    index = stored_readings()
    public = index.about(subject, facets=("registration",))
    expires = _earliest(item.expires for item in public)
    note, note_title = _registrar_note(refused, refusal)
    if expires:
        registrar = next((item.title for item in public if item.title), "")
        source = f"From the public registry, via {registrar}" if registrar else (
            "From the public registry"
        )
        return ZoneInsight(
            label="Registration",
            value=expiry_phrase(expires),
            detail=f"{ended(source)} Auto-renew is unknown without registrar access.",
            note=note,
            note_title=note_title,
        )
    if not refused:
        # The card stays: whether the domain is still owned is its question.
        return ZoneInsight(
            label="Registration",
            value="Not read",
            detail="Neither the registrar nor the public registry has been read yet.",
        )
    unread = index.unread(facets=("registration",))
    return ZoneInsight(
        label="Registration",
        value="Not read",
        detail="No registrar access. The public registry has not been read yet.",
        note=unread[0].detail if unread else note,
        note_title="" if unread else note_title,
    )
