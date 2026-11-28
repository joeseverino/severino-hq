"""What is worth knowing about a domain, contributed one fact at a time.

Restating DNS records says nothing Cloudflare's own dashboard does not; what HQ
can say is how a domain relates to everything else it holds (the services in
it, the certificates covering it, whether its CAA permits HQ's authority). Each
function takes a Zone and returns one insight or None, registered by name in
``zones.ZONE_INSIGHTS`` so a new one is an entry rather than an edit to a page.
"""

import re
from functools import partial

from hq.domains.control_plane.certificate_authorities import UPLOADED
from hq.domains.control_plane.names import in_zone
from hq.domains.control_plane.permissions import Cloudflare
from hq.domains.control_plane.provider_adapters.cloudflare import caa_parts
from hq.domains.control_plane.provider_adapters.contracts import PERMISSION_REFUSAL
from hq.domains.control_plane.provider_adapters.tls import (
    CERTIFICATE_KIND,
    UPLOADED_CERTIFICATE_KIND,
)
from hq.domains.control_plane.provider_spec import expiry_phrase
from hq.domains.control_plane.providers import PROVIDERS, read_spec
from hq.platform.application.labels import dotted, lower_first, sentence
from hq.platform.application.routes import reverse

from .card_items import Badge, ListRow, RowTitle
from .entity_links import entity_link
from .facts import Subject, inventory_about, readings as stored_readings, refusals_about
from .infrastructure import delivery_targets, enabled_resources, resolved_spec
from .known_hosts import operator, registrable
from .mail_policy import is_null_mx
from .moments import expiry_words, moment
from .tone import Absent
from .ui import counted, ended
from .zones import ZONE_KIND, ZoneInsight
lazy from . import zones
lazy from .estate import certificates as expiring
lazy from .service_list import services_by_zone


def _earliest(stamps) -> str:
    """The earliest of several ISO 8601 stamps, or ""."""

    found = [(when, stamp) for stamp in stamps if (when := moment(stamp)) is not None]
    return min(found)[1] if found else ""


def services(zone) -> ZoneInsight | None:
    """What runs in this domain: its services, what serves them, what fronts them.

    Counted from the service catalog, which decides what a service is and
    when one is parked. Readings joined to names in the domain add what serves
    them (a Pages project) and how many sit behind an overlay (Access).
    """

    members = services_by_zone(zones.zone_names()).get(zone.zone, ())
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
            detail="No name here is set up.",
            links=(("Served by", serving),) if serving else (),
            note=overlays,
        )
    parked = [service for service in inside if service.origin and service.origin.parked]
    unhealthy = [service for service in inside if service.faults]
    if len(parked) == len(members):
        handlers = sorted({service.provider_answers for service in parked} - {""})
        value = "Parked"
        detail = ended(f"Handled at {', '.join(handlers)}") if handlers else "Nothing answers there."
    else:
        value = counted(len(members), "service")
        detail = f"{len(unhealthy)} incomplete." if unhealthy else "All set up."
    # A count, with the list one click beneath it: the number opens onto the
    # whole list, and ``url`` reaches a page that does the same job.
    return ZoneInsight(
        label="Services",
        value=value,
        detail=detail,
        url=reverse("control_plane:services"),
        rows=tuple(
            ListRow(
                RowTitle(member.hostname, member.url),
                state=Badge(member.status_label, member.status) if member.status_label else None,
            )
            for member in members
        ),
        links=(("Served by", serving),) if serving else (),
        note=overlays,
    )


def _overlays(joined) -> str:
    """ "2 behind Access": readings that supply no facet, counted by relation."""

    counts: dict[str, set] = {}
    for item in joined:
        if item.facet or not item.spec.joins_hostnames:
            continue
        counts.setdefault(item.relation, set()).update(item.hostnames)
    return dotted(*(f"{len(names)} {lower_first(relation)}" for relation, names in sorted(counts.items())))


def certificates(zone) -> ZoneInsight | None:
    """The managed certificates covering this domain, else what may issue for it.

    A CAA record that refuses the authority HQ renews with is a finding
    (``zone_findings``), drawn with what to change.
    """

    covered = covering(zone)
    permitted = caa_issuers(zone)
    index = stored_readings()
    edge = index.about(Subject.of(zones=(zone.zone,)), facets=("certificate",))
    if not covered:
        if edge:
            return _edge_certificates(edge)
        refused = index.unread(facets=("certificate",))
        if refused:
            return ZoneInsight(
                label="Certificates",
                value=sentence(Absent.NOT_MANAGED),
                detail="Edge certificates are not readable.",
                note=refused[0].detail,
            )
        if permitted:
            return ZoneInsight(
                label="Certificates",
                value=sentence(Absent.NOT_MANAGED),
                detail=ended("Issuance is restricted to " + ", ".join(sorted(permitted))),
            )
        return ZoneInsight(
            label="Certificates",
            value=sentence(Absent.NOT_MANAGED),
            detail="No CAA record either, so any authority may issue for it.",
        )

    resource, _names = covered[0]
    expires = expiry_phrase(str((resource.status or {}).get("not_after", "")))
    extra = f" and {len(covered) - 1} more" if len(covered) > 1 else ""

    # Just the expiry. What it covers is on the certificate's own page.
    return ZoneInsight(
        label="Certificates",
        value=f"{resource.key}{extra}",
        detail=ended(f"Expires {expires}") if expires else "",
        url=entity_link("resource", resource.key).url,
        note=_edge_certificates(edge).detail if edge else "",
    )


def _hostnames(provider, spec) -> tuple[str, ...]:
    return tuple(provider.hostnames(spec))


def covering(zone) -> list:
    """``(resource, names)`` for each managed certificate naming a name in the zone."""

    targets = delivery_targets()
    found = []
    # From the read the page already shares, in its order.
    for resource in enabled_resources():
        if resource.kind not in (CERTIFICATE_KIND, UPLOADED_CERTIFICATE_KIND):
            continue
        provider = PROVIDERS.get(resource.kind)
        if provider is None or provider.hostnames is None:
            continue
        names = read_spec(resource.kind, resolved_spec(resource, targets), partial(_hostnames, provider))
        if names is None:
            continue
        if any(in_zone(name, zone.zone) for name in names):
            found.append((resource, names))
    return found


def security(zone) -> ZoneInsight | None:
    """How this domain answers over TLS, and with which certificates.

    One card: the TLS mode, the edge and managed certificates and the earliest
    expiry among them. What is wrong with them is a finding about the domain.
    """

    tls = posture(zone)
    certificates_card = certificates(zone)
    edge = stored_readings().about(Subject.of(zones=(zone.zone,)), facets=("certificate",))
    covered = covering(zone)
    # The mode is the answer; the rest is one line of facts. What each mode
    # means and who issued each certificate are the certificates' own pages.
    facts = list(_posture_facts(zone))
    if found := expiring((zone.zone,), keys={resource.key for resource, _ in covered}):
        facts.append(f"{counted(len(found), 'certificate')}, first {expiry_words(found[0].expires)}")
    return ZoneInsight(
        label="Security",
        value=tls.value if tls else "TLS not read",
        detail=dotted(*facts),
        url=certificates_card.url if certificates_card is not None else "",
        # Only why a part could not be read; a readable edge is in the count.
        note=certificates_card.note if certificates_card is not None and not edge else "",
    )


def _edge_certificates(edge) -> ZoneInsight:
    """Edge certificates covering the zone: how many, the earliest expiry, who issued them."""

    earliest = _earliest(item.expires for item in edge)
    issuers = sorted({item.issuer for item in edge if item.issuer and item.issuer != UPLOADED})
    uploaded = sum(item.issuer == UPLOADED for item in edge)
    detail = []
    if earliest:
        detail.append(f"Earliest expires {expiry_phrase(earliest)}.")
    if issuers:
        detail.append(ended(f"Issued by {', '.join(issuers)}"))
    if uploaded:
        detail.append(f"{counted(uploaded, 'certificate')} {UPLOADED}.")
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
        key=lambda r: r.priority if r.priority is not None else 0,
    )
    spf = [r for r in zone.records if r.record_type == "TXT" and "v=spf1" in r.content.lower()]
    dmarc = [r for r in zone.records if r.record_type == "TXT" and r.name.startswith("_dmarc.")]

    if not (mail or spf or dmarc):
        return ZoneInsight(
            label="Email",
            url=reverse("zones:mail", kwargs={"zone": zone.zone}),
            value=sentence(Absent.NOT_SET_UP),
            detail=(
                "No MX, SPF or DMARC record. This domain receives no mail, and "
                "nothing stops anyone sending mail that says it comes from it."
            ),
            note="Next: publish v=spf1 -all and a DMARC p=reject record.",
        )

    # A null MX names no server: the domain says it takes no mail.
    refuses = is_null_mx(tuple(mail))
    if refuses:
        mail = []
    # Assembled as whole sentences rather than joined fragments: capitalizing
    # a comma-joined list lowercases the acronyms and states nothing clearly.
    sentences = []
    if not mail:
        sentences.append("A null MX says it takes no mail." if refuses else "Nothing accepts mail for this domain.")
    sentences.append("SPF is published." if spf else "No SPF record.")
    sentences.append(ended(_dmarc_policy(dmarc[0].content)) if dmarc else "No DMARC record.")
    return ZoneInsight(
        label="Email",
        url=reverse("zones:mail", kwargs={"zone": zone.zone}),
        value=_mail_host(mail) if mail else "Not received",
        detail=" ".join(sentences),
    )


def _mail_host(mail) -> str:
    """Who actually receives mail for this domain, read off the MX records.

    "2 mail servers" is a true and useless answer: the count of MX records is
    a redundancy detail, and the question is who has the mailbox.
    """

    hosts = {registrable(record.content) for record in mail}
    if len(hosts) != 1:
        # Split across operators, unusual enough to state plainly rather than
        # summarize into one name that would be half wrong.
        return f"{len(mail)} mail servers"
    return operator(hosts.pop())


def caa_issuers(zone) -> set[str]:
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
TLS_MODES = {
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


def _posture_record(zone) -> dict[str, str]:
    found: dict[str, str] = {}
    for _snapshot, record in inventory_about(ZONE_KIND, Subject.of(hostnames=(zone.zone,))):
        found = dict(record.get("posture") or {})
    return found


def _posture_facts(zone) -> tuple[str, ...]:
    """The posture beyond its mode, a few words each: "TLS 1.3+", "HTTPS forced"."""

    found = _posture_record(zone)
    minimum = str(found.get("min_tls_version", "")).strip()
    return tuple(
        fact
        for fact in (
            f"TLS {minimum}+" if minimum else "",
            "HTTPS forced" if found.get("always_use_https") == "on" else "",
        )
        if fact
    )


def posture(zone) -> ZoneInsight | None:
    """How this domain answers over TLS, as Cloudflare currently holds it.

    Stated here; one weaker than another domain's is a finding about the
    domain (``zone_findings``). A posture HQ could not read is never shown as
    "Off".
    """

    found = _posture_record(zone)
    refused = _zone_part_refused(zone.zone, "posture")
    # A refused read says so; a domain whose posture HQ could not read is not
    # one served over plain HTTP, and "Off" because a token lacked a
    # permission is worse than no card.
    if refused is not None:
        return ZoneInsight(
            label="TLS posture",
            value="Not readable",
            detail=ended(refused.phrase),
        )
    if not found:
        return None

    mode = str(found.get("ssl", "")).lower()
    label, explanation = TLS_MODES.get(mode, (mode.replace("_", " ").title(), ""))
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

    subject = Subject.of(hostnames=(zone.zone,))
    found: dict[str, object] = {}
    for _snapshot, record in inventory_about(ZONE_KIND, subject):
        found = dict(record.get("registration") or {})
    part = _zone_part_refused(zone.zone, "registration")
    refused = (part.reason or part.phrase) if part is not None else ""
    expires = "" if part is not None else str(found.get("expires_at", ""))
    if not expires:
        return _public_registration(subject, refused, part.refusal if part is not None else "")
    when = moment(expires)
    if when is None:
        return None
    renews = bool(found.get("auto_renew"))
    # The registrar reports its own name beside the dates; say it rather than
    # "the registrar", which makes the reader go and find out which one.
    registrar = str(found.get("registrar", "") or "").strip()
    return ZoneInsight(
        label="Registration",
        value=expiry_phrase(when),
        detail=(
            ended(f"Renews itself through {registrar or 'the registrar'}")
            if renews
            else f"Auto-renew is off{f' at {registrar}' if registrar else ''}, so this has to be renewed by hand."
        ),
    )


def _zone_part_refused(zone: str, part: str):
    """The zone sweep's refusal of one part on this zone, or None."""

    return next(
        (
            refused
            for refused in refusals_about(Subject.of(hostnames=(zone,)), (ZONE_KIND,))
            if refused.part.name == part
        ),
        None,
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
        return f"Add {Cloudflare.REGISTRAR_DOMAINS_READ} to see auto-renew.", refused
    return f"Registrar not read: {refused}", ""


def _public_registration(subject, refused: str, refusal: str = "") -> ZoneInsight | None:
    """The public registry's expiry, when the registrar's could not be read."""

    index = stored_readings()
    public = index.about(subject, facets=("registration",))
    expires = _earliest(item.expires for item in public)
    note, note_title = _registrar_note(refused, refusal)
    if expires:
        registrar = next((item.title for item in public if item.title), "")
        source = f"From the public registry, via {registrar}" if registrar else ("From the public registry")
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
