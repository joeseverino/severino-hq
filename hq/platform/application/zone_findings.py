"""Findings about a domain: what its records or its posture leave wrong.

Each is about the domain itself (``zone:`` and its name), so its own page
draws it with what to change, and the domain's cards state facts only. A
finding about something in the domain (a service's setup, the connection that
reads it) is that thing's own rule's.
"""

from hq.domains.control_plane.names import normalized_hostname
from hq.domains.control_plane.provider_adapters.tls import CERTIFICATE_KIND
from hq.platform.application.routes import reverse

from .display_words import Code
from .entity_links import entity_link
from .finding_model import Finding, FindingEstate, FindingRule, opens
from .finding_steps import Do
from .references import Kind
from .tone import Tone
from .ui import counted, ended
lazy from .facts import inventory_records
lazy from .mail_policy import SPF_LOOKUP_LIMIT, mail_overview
lazy from .zone_insights import TLS_MODES, caa_issuers, covering
lazy from .zones import ZONE_KIND, zone_catalog

ACME_LEFT_OVER = "acme-challenge-left-over"
CAA_REFUSES_RENEWAL = "caa-refuses-renewal"
TLS_WEAKER = "encryption-weaker-than-elsewhere"
SPF_OVER_LIMIT = "spf-over-lookup-limit"
DMARC_NOT_ENFORCING = "dmarc-not-enforcing"

# The authority HQ's own certificate provider issues from, as a CAA record
# names it: that provider's summary says "Let's Encrypt" as prose.
MANAGED_ISSUER = "letsencrypt.org"

# The card of a domain's pages that draws a rule about its mail: the Email page's.
MAIL_CARD = "mail"

# Cloudflare's documented order of SSL modes, weakest first.
_SSL_ORDER = ("off", "flexible", "full", "strict")


def _zones(estate: FindingEstate) -> tuple:
    """The domains the estate holds, read only where it holds one: an estate
    with no domain buys no query to learn it."""

    named = {node.label for node in estate.nodes() if node.kind == "zone"}
    return tuple(zone for zone in zone_catalog() if zone.zone in named) if named else ()


def _left_over(estate: FindingEstate) -> tuple[Finding, ...]:
    """Challenge records that outlived the issuance they existed for.

    One judgment that needs no declared policy: a challenge record exists for
    the seconds an authority takes to verify a request. One still present at a
    sweep was left behind and serves nothing.
    """

    found = []
    for zone in _zones(estate):
        stale = sorted(
            {
                record.name
                for record in zone.records
                if record.record_type == "TXT" and record.name.startswith("_acme-challenge.")
            }
        )
        if not stale:
            continue
        found.append(
            Finding(
                rule=ACME_LEFT_OVER,
                subject=Kind.ZONE.of(zone.zone),
                title=f"{zone.zone} has {counted(len(stale), 'left-over ACME challenge', 'left-over ACME challenges')}",
                severity=Tone.ATTENTION,
                explanation="A challenge record is made while a certificate is issued and removed after; these serve nothing.",
                evidence=tuple(("Record", Code(name)) for name in stale),
                fixes=(Do("Delete each record named here at the DNS provider."),),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


def _caa_refuses(estate: FindingEstate) -> tuple[Finding, ...]:
    """A CAA record that refuses the authority HQ renews a certificate here with.

    A security control that turns into an outage on the day of the next
    renewal: nothing at the DNS provider knows which certificates HQ renews,
    and nothing in the certificate registry knows what the zone permits.
    Only a certificate HQ issues has an authority HQ can predict.
    """

    found = []
    for zone in _zones(estate):
        permitted = caa_issuers(zone)
        renewed = [resource for resource, _names in covering(zone) if resource.kind == CERTIFICATE_KIND]
        if not renewed or not permitted or MANAGED_ISSUER in permitted:
            continue
        certificate = entity_link("resource", renewed[0].key)
        found.append(
            Finding(
                rule=CAA_REFUSES_RENEWAL,
                subject=Kind.ZONE.of(zone.zone),
                title=f"{zone.zone}'s CAA records refuse {MANAGED_ISSUER}",
                severity=Tone.ATTENTION,
                explanation=f"The next renewal of {renewed[0].key} will be refused, and the certificate lapses.",
                evidence=(
                    ("Permits", ", ".join(sorted(permitted))),
                    *(("Renewed by HQ", entity_link("resource", resource.key)) for resource in renewed),
                ),
                fixes=(Do(ended(f"Add this CAA record to {zone.zone}"), value=f'0 issue "{MANAGED_ISSUER}"'),),
                primary=opens("Open certificate", certificate.url),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


def _strength(found: dict) -> tuple[int, tuple[int, ...]] | None:
    """A readable posture as (mode rank, minimum TLS version), or None."""

    if not found:
        return None
    mode = str(found.get("ssl", "")).lower().replace("full_strict", "strict")
    if mode not in _SSL_ORDER:
        return None
    version = tuple(int(part) for part in str(found.get("min_tls_version", "") or "0").split(".") if part.isdigit())
    return _SSL_ORDER.index(mode), version


def _posture_words(found: dict) -> tuple[str, str]:
    """A posture's mode and minimum version, as Cloudflare's settings name them."""

    mode = TLS_MODES.get(str(found.get("ssl", "")).lower(), ("", ""))[0]
    return mode, str(found.get("min_tls_version", "") or "").strip()


def _said(found: dict) -> str:
    mode, minimum = _posture_words(found)
    return f"{mode}, TLS {minimum}" if minimum else mode


def _weaker(estate: FindingEstate) -> tuple[Finding, ...]:
    """A domain answering with a weaker TLS posture than another domain here.

    The weaker one is the easier way in, and the stronger one shows the
    posture the estate already holds itself to.
    """

    if not any(node.kind == "zone" for node in estate.nodes()):
        return ()
    postures: dict[str, dict] = {}
    for _snapshot, record in inventory_records(ZONE_KIND):
        if name := normalized_hostname(record.get("zone")):
            postures[name] = dict(record.get("posture") or {})
    found = []
    for name, mine in sorted(postures.items()):
        strength = _strength(mine)
        if strength is None:
            continue
        stronger = [
            (theirs, other, posture)
            for other, posture in sorted(postures.items())
            if other != name and (theirs := _strength(posture)) is not None and theirs > strength
        ]
        if not stronger:
            continue
        _rank, best, target = max(stronger, key=lambda each: each[0])
        mode, minimum = _posture_words(target)
        change = f"set the encryption mode to {mode}" + (
            f" and the minimum TLS version to {minimum}" if minimum else ""
        )
        found.append(
            Finding(
                rule=TLS_WEAKER,
                subject=Kind.ZONE.of(name),
                title=f"{name} accepts weaker TLS than {best}",
                severity=Tone.ATTENTION,
                explanation="A domain held to less than the others is the easier way in.",
                evidence=(
                    ("Posture", _said(mine) or "Not read"),
                    *(("Stronger on", f"{other} ({_said(posture)})") for _theirs, other, posture in stronger),
                ),
                fixes=(Do(ended(f"In Cloudflare's SSL/TLS settings for {name}, {change}, as on {best}")),),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


def _mail_page(zone) -> str:
    return reverse("zones:mail", kwargs={"zone": zone.zone})


def _spf_over(estate: FindingEstate) -> tuple[Finding, ...]:
    """An SPF record that needs more DNS lookups than a receiver makes.

    Past the limit a receiver stops evaluating and returns an error, so the
    policy silently stops applying: nothing bounces and nothing warns.
    """

    found = []
    for zone in _zones(estate):
        spf = mail_overview(zone).spf
        if spf is None or not spf.over_limit:
            continue
        found.append(
            Finding(
                rule=SPF_OVER_LIMIT,
                subject=Kind.ZONE.of(zone.zone),
                title=f"{zone.zone}'s SPF needs {counted(spf.lookups, 'DNS lookup', 'DNS lookups')}",
                severity=Tone.ATTENTION,
                explanation=f"Receivers stop after {SPF_LOOKUP_LIMIT}, and the policy silently stops applying.",
                evidence=(("Lookups", f"{spf.lookups} of {SPF_LOOKUP_LIMIT}"),),
                fixes=(
                    Do(
                        f"Remove senders from the SPF record, or name their addresses in place of an include, "
                        f"until it needs {SPF_LOOKUP_LIMIT} lookups or fewer."
                    ),
                ),
                primary=opens("Open senders", f"{_mail_page(zone)}#senders"),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


def _dmarc_not_enforcing(estate: FindingEstate) -> tuple[Finding, ...]:
    """A DMARC record that asks receivers to deliver mail failing its checks."""

    found = []
    for zone in _zones(estate):
        if mail_overview(zone).dmarc_tags.get("p") != "none":
            continue
        found.append(
            Finding(
                rule=DMARC_NOT_ENFORCING,
                subject=Kind.ZONE.of(zone.zone),
                title=f"{zone.zone}'s DMARC delivers mail that fails it",
                severity=Tone.ATTENTION,
                explanation="Mail forged in its name reaches inboxes; the policy only reports it.",
                evidence=(("Policy", "Deliver it anyway"),),
                fixes=(
                    Do(
                        "Once the reports show every real sender passing, set what happens to failing mail "
                        "to Send it to spam or Reject it outright."
                    ),
                ),
                primary=opens("Open failed mail", f"{_mail_page(zone)}#failed-mail"),
            )
        )
    return tuple(sorted(found, key=lambda finding: finding.title))


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        ACME_LEFT_OVER,
        "Left-over ACME challenges",
        Tone.ATTENTION,
        _left_over,
        operator_action="Delete the left-over challenge records at the DNS provider.",
        no_help_reason="HQ never declares a challenge record, so it has none to remove.",
        bars={Tone.ATTENTION: "a challenge record outlived the issuance it was made for"},
        clears="the challenge records are deleted",
        gathered=True,
    ),
    FindingRule(
        CAA_REFUSES_RENEWAL,
        "CAA refuses a renewal",
        Tone.ATTENTION,
        _caa_refuses,
        operator_action="Add a CAA record that permits the authority HQ renews with.",
        no_help_reason="HQ does not change a domain's CAA policy for you.",
        bars={Tone.ATTENTION: "a domain's CAA records refuse the authority a certificate HQ renews there needs"},
        clears="the CAA records permit that authority",
        gathered=True,
    ),
    FindingRule(
        TLS_WEAKER,
        "Weaker encryption than elsewhere",
        Tone.ATTENTION,
        _weaker,
        operator_action="Raise the domain's TLS posture at Cloudflare to the strongest one held here.",
        no_help_reason="HQ's credential reads a domain's TLS settings and cannot change them.",
        bars={Tone.ATTENTION: "another domain here is held to a stronger TLS posture"},
        clears="no domain here holds a stronger posture",
        gathered=True,
    ),
    FindingRule(
        SPF_OVER_LIMIT,
        "SPF over its lookup limit",
        Tone.ATTENTION,
        _spf_over,
        operator_action=f"Cut the SPF record to {SPF_LOOKUP_LIMIT} DNS lookups or fewer.",
        no_help_reason="Which senders to keep is yours to decide.",
        card=MAIL_CARD,
        bars={Tone.ATTENTION: f"an SPF record needs more than {SPF_LOOKUP_LIMIT} DNS lookups"},
        clears=f"the SPF record needs {SPF_LOOKUP_LIMIT} lookups or fewer",
        gathered=True,
    ),
    FindingRule(
        DMARC_NOT_ENFORCING,
        "DMARC not enforcing",
        Tone.ATTENTION,
        _dmarc_not_enforcing,
        operator_action="Set the DMARC policy to quarantine or reject on the domain's Email page.",
        no_help_reason="When every real sender passes is yours to judge from the reports.",
        card=MAIL_CARD,
        bars={Tone.ATTENTION: "a domain's DMARC policy delivers mail that fails it"},
        clears="the policy quarantines or rejects failing mail",
        gathered=True,
    ),
)
