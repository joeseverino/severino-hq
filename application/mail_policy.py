"""What a domain says about its own mail, as something other than a string.

SPF and DMARC are policies published as TXT records, and a TXT record is where
they stop being readable. `v=DMARC1; p=reject; sp=reject; rua=mailto:...` is a
decision about what happens to forged mail, written in a notation that hides
which decision was made -- so it gets copied from a blog post once and never
looked at again.

The grammar is declared here, once, and everything else derives from it: the
controls a form renders, the sentence a card reads out, the value written back
to DNS, and what each choice actually does. Adding a tag is an entry in a
tuple, not an edit to a parser, a form and a template that must agree.

Nothing here talks to DNS. A policy is parsed from a record's value and composed
back into one; publishing it is the DNS record's own use case, which already
knows how to reconcile a change and say what it will cost.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DMARC_VERSION = "DMARC1"
SPF_VERSION = "spf1"

# RFC 7208 §4.6.4. Every `include`, `a`, `mx`, `ptr`, `exists` and `redirect`
# costs a DNS lookup, and a policy needing more than ten is not evaluated --
# receivers return permerror and the protection silently stops applying.
SPF_LOOKUP_LIMIT = 10
SPF_LOOKUP_MECHANISMS = ("include", "a", "mx", "ptr", "exists", "redirect")


@dataclass(frozen=True)
class Choice:
    """One selectable answer, and what choosing it actually does."""

    value: str
    label: str
    consequence: str


@dataclass(frozen=True)
class PolicyTag:
    """One field of a policy record.

    ``sentence`` renders this tag's contribution to the plain-English summary,
    so the summary and the record are two projections of one value rather than
    two descriptions maintained apart.
    """

    id: str
    label: str
    kind: str  # "choice" | "addresses" | "percent"
    help: str = ""
    default: str = ""
    choices: tuple[Choice, ...] = ()
    required: bool = False
    sentence: Any = None

    def describe(self, value: str) -> str:
        if not value or self.sentence is None:
            return ""
        return self.sentence(value)


def _policy_sentence(value: str) -> str:
    return {
        "none": "Mail that fails checks is delivered anyway, and only reported.",
        "quarantine": "Mail that fails checks is sent to spam.",
        "reject": "Mail that fails checks is rejected outright.",
    }.get(value, "")


def _subdomain_sentence(value: str) -> str:
    return {
        "none": "Subdomains are exempt: forged mail from them is delivered.",
        "quarantine": "Subdomains send failing mail to spam.",
        "reject": "Subdomains reject failing mail too.",
    }.get(value, "")


def _reports_sentence(value: str) -> str:
    count = len(_addresses(value))
    if not count:
        return ""
    return "Aggregate reports are sent." if count == 1 else f"Aggregate reports go to {count} addresses."


def _percent_sentence(value: str) -> str:
    if value in ("", "100"):
        return ""
    return f"The policy is applied to {value}% of failing mail; the rest is only reported."


DMARC_TAGS: tuple[PolicyTag, ...] = (
    PolicyTag(
        id="p",
        label="When a message fails the checks",
        kind="choice",
        required=True,
        default="none",
        help="What receivers should do with mail that claims to be from this domain and cannot prove it.",
        choices=(
            Choice(
                "none",
                "Deliver it anyway",
                "Nothing is blocked. Use this while you read the reports and "
                "find out who legitimately sends as you.",
            ),
            Choice(
                "quarantine",
                "Send it to spam",
                "Forged mail lands in junk rather than the inbox. A sender you "
                "have not authorised yet gets filtered instead of lost.",
            ),
            Choice(
                "reject",
                "Reject it outright",
                "Forged mail is refused at the door. Strongest protection, and "
                "an unauthorised legitimate sender stops being delivered.",
            ),
        ),
        sentence=_policy_sentence,
    ),
    PolicyTag(
        id="sp",
        label="For subdomains",
        kind="choice",
        default="",
        help="Left unset, subdomains follow the rule above.",
        choices=(
            Choice("", "Same as above", "Subdomains inherit the domain's policy."),
            Choice("none", "Deliver it anyway", "Subdomains are left unprotected."),
            Choice("quarantine", "Send it to spam", "Subdomains filter failing mail."),
            Choice("reject", "Reject it outright", "Subdomains refuse failing mail."),
        ),
        sentence=_subdomain_sentence,
    ),
    PolicyTag(
        id="rua",
        label="Send aggregate reports to",
        kind="addresses",
        help="Daily summaries of who is sending as this domain. This is how a policy is made safe to tighten.",
        sentence=_reports_sentence,
    ),
    PolicyTag(
        id="pct",
        label="Apply the policy to",
        kind="percent",
        default="100",
        help="A way to tighten gradually. Anything under 100% leaves the rest reported only.",
        sentence=_percent_sentence,
    ),
)

DMARC_BY_ID = {tag.id: tag for tag in DMARC_TAGS}


def _addresses(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in str(value).split(",") if part.strip())


def unquote(value: str) -> str:
    """A TXT record's value without the quoting DNS carries it in."""

    text = str(value or "").strip()
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        text = text[1:-1]
    # A long TXT is published as adjacent quoted strings; joined, they are one
    # policy, and split they parse as neither.
    return text.replace('" "', "").strip()


def parse_dmarc(value: str) -> dict[str, str]:
    """A DMARC record as its tags. Unknown tags are kept, never dropped.

    Dropping one would mean an editor silently deleting a policy it did not
    happen to model -- the record is the operator's, not this module's.
    """

    tags: dict[str, str] = {}
    for part in unquote(value).split(";"):
        name, _, raw = part.partition("=")
        name, raw = name.strip().lower(), raw.strip()
        if name and raw:
            tags[name] = raw
    return tags


def compose_dmarc(tags: dict[str, str]) -> str:
    """The record a set of tags publishes as.

    ``v`` leads because receivers require it first. Everything else keeps the
    declared order, then anything unrecognised, so a round trip through this
    module never reorders a tag it did not put there.
    """

    ordered: list[tuple[str, str]] = [("v", DMARC_VERSION)]
    for tag in DMARC_TAGS:
        value = str(tags.get(tag.id, "") or "").strip()
        if not value:
            continue
        if tag.default and value == tag.default and not tag.required:
            continue
        ordered.append((tag.id, value))
    for name, value in tags.items():
        if name != "v" and name not in DMARC_BY_ID and str(value).strip():
            ordered.append((name, str(value).strip()))
    return "; ".join(f"{name}={value}" for name, value in ordered)


def describe_dmarc(value: str) -> tuple[str, ...]:
    """The policy as sentences, in the order they matter."""

    tags = parse_dmarc(value)
    if tags.get("v", "").upper() != DMARC_VERSION:
        return ()
    said = []
    for tag in DMARC_TAGS:
        sentence = tag.describe(tags.get(tag.id, ""))
        if sentence:
            said.append(sentence)
    if "sp" not in tags and tags.get("p"):
        said.append("Subdomains follow the same rule.")
    if not tags.get("rua"):
        said.append("No reports are collected, so nothing shows who sends as this domain.")
    return tuple(said)


@dataclass(frozen=True)
class SpfTerm:
    qualifier: str
    mechanism: str
    argument: str

    @property
    def costs_lookup(self) -> bool:
        return self.mechanism in SPF_LOOKUP_MECHANISMS

    def __str__(self) -> str:
        prefix = "" if self.qualifier == "+" else self.qualifier
        return f"{prefix}{self.mechanism}" + (f":{self.argument}" if self.argument else "")


@dataclass(frozen=True)
class SpfPolicy:
    terms: tuple[SpfTerm, ...] = ()
    valid: bool = True

    @property
    def lookups(self) -> int:
        return sum(1 for term in self.terms if term.costs_lookup)

    @property
    def over_limit(self) -> bool:
        """Past ten lookups a receiver stops evaluating and the policy does nothing.

        Worth saying out loud: this fails silently. Nothing bounces, nothing
        warns, and the protection is simply not applied.
        """

        return self.lookups > SPF_LOOKUP_LIMIT

    @property
    def default_result(self) -> str:
        for term in reversed(self.terms):
            if term.mechanism == "all":
                return {
                    "-": "Anything else is rejected.",
                    "~": "Anything else is marked as a soft failure.",
                    "?": "Anything else is treated as neutral, which protects nothing.",
                    "+": "Anything else passes, which protects nothing.",
                }.get(term.qualifier, "")
        return "No `all` term, so senders not listed here are simply unhandled."


def parse_spf(value: str) -> SpfPolicy:
    text = unquote(value)
    words = text.split()
    if not words or words[0].lower() != f"v={SPF_VERSION}":
        return SpfPolicy(valid=False)
    terms = []
    for word in words[1:]:
        qualifier = word[0] if word[:1] in "+-~?" else "+"
        body = word[1:] if word[:1] in "+-~?" else word
        mechanism, _, argument = body.partition(":")
        terms.append(SpfTerm(qualifier, mechanism.lower(), argument))
    return SpfPolicy(terms=tuple(terms))


def compose_spf(terms: tuple[SpfTerm, ...]) -> str:
    return " ".join([f"v={SPF_VERSION}", *(str(term) for term in terms)])


SPF_DEFAULTS: tuple[Choice, ...] = (
    Choice(
        "-",
        "Reject it",
        "Anyone not listed above is refused. The strongest answer, and the one "
        "DMARC needs to mean anything.",
    ),
    Choice(
        "~",
        "Mark it as suspicious",
        "A soft failure: receivers usually accept it and flag it. Useful while "
        "you are still finding senders.",
    ),
    Choice(
        "?",
        "No opinion",
        "Neutral. Says nothing about unlisted senders, which protects nothing.",
    ),
    Choice(
        "+",
        "Allow it",
        "Every sender passes. This is the same as publishing no SPF at all.",
    ),
)


@dataclass(frozen=True)
class MailSection:
    """One stage of a domain's mail, with the records that decide it."""

    id: str
    label: str
    question: str
    answer: str
    detail: str
    records: tuple[Any, ...] = ()
    concern: str = ""
    add_type: str = ""


@dataclass(frozen=True)
class MailOverview:
    """Receiving, sending, signing, enforcing -- in the order mail flows."""

    zone: str
    sections: tuple[MailSection, ...]
    dmarc_record: Any = None
    dmarc_tags: dict[str, str] = field(default_factory=dict)

    @property
    def concerns(self) -> tuple[str, ...]:
        return tuple(section.concern for section in self.sections if section.concern)


def _is_spf(record) -> bool:
    return record.record_type == "TXT" and unquote(record.content).lower().startswith(
        f"v={SPF_VERSION}"
    )


def _is_dmarc(record) -> bool:
    return record.record_type == "TXT" and record.name.lower().startswith("_dmarc.")


def _is_dkim(record) -> bool:
    return "._domainkey." in record.name.lower()


def mail_overview(zone) -> MailOverview:
    """Everything that decides this domain's mail, assembled once.

    Four records that are read separately and only make sense together: who
    receives, who may send, what signs, and what happens when a message proves
    none of it. Each section says what it answers, so a gap reads as a missing
    answer rather than a missing record.
    """

    records = tuple(zone.records)
    mx = tuple(
        sorted(
            (r for r in records if r.record_type == "MX"),
            key=lambda r: (r.priority if r.priority is not None else 0),
        )
    )
    spf_records = tuple(r for r in records if _is_spf(r))
    dkim = tuple(sorted((r for r in records if _is_dkim(r)), key=lambda r: r.name))
    dmarc_records = tuple(r for r in records if _is_dmarc(r))

    spf = parse_spf(spf_records[0].content) if spf_records else None
    dmarc_value = dmarc_records[0].content if dmarc_records else ""
    dmarc_tags = parse_dmarc(dmarc_value) if dmarc_records else {}
    policy = dmarc_tags.get("p", "")

    sections = (
        MailSection(
            id="receiving",
            label="Receiving",
            question="Who accepts mail for this domain?",
            answer=(
                ", ".join(sorted({r.content for r in mx}))
                if mx
                else "Nobody"
            ),
            detail=(
                f"{len(mx)} mail server{'s' if len(mx) != 1 else ''}, tried in priority order."
                if mx
                else "No MX record, so mail sent to this domain is not delivered anywhere."
            ),
            records=mx,
            add_type="MX",
        ),
        MailSection(
            id="sending",
            label="Sending",
            question="Who is allowed to send as this domain?",
            answer=(
                f"{len(spf.terms)} rule{'s' if len(spf.terms) != 1 else ''}"
                if spf and spf.valid
                else "Anyone"
            ),
            detail=(
                spf.default_result
                if spf and spf.valid
                else "No SPF record. Nothing states who may send as this domain."
            ),
            records=spf_records,
            concern=(
                f"{spf.lookups} DNS lookups, over the limit of {SPF_LOOKUP_LIMIT}. "
                "Receivers stop evaluating and the policy silently stops applying."
                if spf and spf.over_limit
                else ""
            ),
            add_type="TXT",
        ),
        MailSection(
            id="signing",
            label="Signing",
            question="What proves a message really came from here?",
            answer=(
                f"{len(dkim)} key{'s' if len(dkim) != 1 else ''}"
                if dkim
                else "Nothing"
            ),
            detail=(
                "Published by the mail provider. HQ keeps the records; the keys "
                "themselves are theirs to rotate."
                if dkim
                else "No DKIM record, so nothing signs mail from this domain."
            ),
            records=dkim,
            add_type="CNAME",
        ),
        MailSection(
            id="enforcing",
            label="Enforcing",
            question="What happens to mail that fails these checks?",
            answer={
                "reject": "Rejected",
                "quarantine": "Sent to spam",
                "none": "Delivered anyway",
            }.get(policy, "Nothing"),
            detail=(
                " ".join(describe_dmarc(dmarc_value))
                if dmarc_records
                else "No DMARC record. Anyone can send mail claiming to be this "
                "domain and receivers have no instruction to refuse it."
            ),
            records=dmarc_records,
            concern=(
                "Published but not enforcing: failures are delivered."
                if policy == "none"
                else ""
            ),
            add_type="TXT",
        ),
    )
    return MailOverview(
        zone=zone.zone,
        sections=sections,
        dmarc_record=dmarc_records[0] if dmarc_records else None,
        dmarc_tags=dmarc_tags,
    )
