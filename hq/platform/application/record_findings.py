"""Findings about HQ's own records: what a record section holds that waits on a person.

A backlog is one finding that counts its records (drafts not published,
expenses with no receipt), raised under the section that holds them and told only to a
reader of what that section declares (``FindingRule.domain``).
A record whose reference names nothing is one finding of its own.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from django.db.models import Count, Q

from hq.domains.assets.models import Asset
from hq.domains.calendars.models import Entry
from hq.domains.contacts import inbox
from hq.domains.content.models import ContentItem
from hq.domains.expenses.models import Expense
from hq.domains.receipts.models import Receipt
from hq.platform.application.routes import reverse
from hq.platform.application.url_query import with_query

from . import sections
from .entity_links import EntityLink, entity_link
from .finding_model import Finding, FindingEstate, FindingRule, opens
from .projection import read_once
from .references import UNLINK, naming_nothing
from .tone import Tone
from .ui import counted, ended

# How many of a backlog's things a finding says by name before it counts the rest.
MOST_NAMED = 3
CONTACTS_STATE_KEY = "attention.contacts-state"

CONTENT_DRAFTS = "content-drafts"
CONTENT_UNDOCUMENTED = "content-undocumented"
CONTACTS_UNREAD = "contacts-unread"
EXPENSES_WITHOUT_RECEIPTS = "expenses-without-receipts"
RECEIPTS_UNLINKED = "receipts-unlinked"
ASSETS_MISSING_PURCHASE = "assets-missing-purchase"


def contacts_state() -> tuple[int, str]:
    """Unread submissions and whether the last read reached D1. No network:
    the snapshot and the queue both ask here, so they cannot disagree."""

    return read_once(CONTACTS_STATE_KEY, inbox.unread)


@dataclass(frozen=True, slots=True)
class _Backlog:
    """What a section's backlog counts, and how its one finding says it.

    The title is the count and what is true of them, said whole for ``one``
    and for ``many``. ``named`` reads the records themselves, which then are
    the count: the first few are said by name, each a link to its own page,
    and the rest are a count."""

    one: str
    many: str
    counts: str
    opens_as: str
    route: str
    query: Mapping[str, str]
    count: Callable[[], int] | None = None
    named: Callable[[], tuple[EntityLink, ...]] | None = None
    said: str = ""
    # Work in hand rather than a problem to fix.
    notice: bool = False

    def found(self, rule: str) -> tuple[Finding, ...]:
        named = self.named() if self.named is not None else ()
        count = len(named) if self.named is not None else self.count() if self.count is not None else 0
        if not count:
            return ()
        first = named[:MOST_NAMED]
        more = count - len(first)
        names = ", ".join(link.label for link in first) + (
            f" and {counted(more, 'other')}" if first and more > 0 else ""
        )
        return (
            Finding(
                rule=rule,
                subject="",
                title=counted(count, self.one, self.many),
                severity=Tone.ATTENTION,
                explanation=" ".join(part for part in (ended(names), self.said) if part),
                evidence=((self.counts, f"{count:,}"),),
                primary=opens(self.opens_as, with_query(reverse(self.route), self.query)),
                offers=tuple(opens(link.label, link.url) for link in first if link.url),
                count=count,
                notice=self.notice,
            ),
        )


def _backlog(
    rule: str, title: str, backlog: _Backlog, *, domain: str, operator_action: str, no_help_reason: str, bar: str
) -> FindingRule:
    """The rule of a section's backlog: one finding when the section holds records to work on, none when it holds none."""

    def detect(_estate: FindingEstate) -> tuple[Finding, ...]:
        return backlog.found(rule)

    return FindingRule(
        rule,
        title,
        Tone.ATTENTION,
        detect,
        operator_action=operator_action,
        no_help_reason=no_help_reason,
        bars={Tone.ATTENTION: bar},
        domain=domain,
        gathered=True,
    )


def _unread() -> int:
    """Unread submissions from the public site, as the last read of its store
    left them. An unreachable store reports nothing outstanding: the outage is
    its connection's to report."""

    return contacts_state()[0]


def _unpriced() -> tuple[EntityLink, ...]:
    """Each active asset with no purchase date or no cost, by name."""

    return tuple(
        entity_link("asset", slug, label=name)
        for name, slug in Asset.objects.filter(
            Q(status=Asset.Status.ACTIVE) & (Q(purchase_date__isnull=True) | Q(total_cost=0))
        )
        .order_by("item_name")
        .values_list("item_name", "slug")
    )


def _links_to_nothing(rule: str, model: type) -> FindingRule:
    """The rule of a record section whose rows may link to something HQ no longer has: one finding per row."""

    def detect(_estate: FindingEstate) -> tuple[Finding, ...]:
        return tuple(
            Finding(
                rule=rule,
                subject="",
                part=found.key,
                title=found.title,
                severity=Tone.ATTENTION,
                explanation=UNLINK,
                primary=opens("Open record", found.url),
            )
            for found in naming_nothing(model)
        )

    return FindingRule(
        rule,
        "Links to nothing",
        Tone.ATTENTION,
        detect,
        operator_action="Open the record, then choose something else for the link or clear it.",
        no_help_reason="Only a person knows what the record was meant to name.",
        bars={Tone.ATTENTION: "a record links to something HQ no longer has, so the record points nowhere"},
        domain={Expense: "hq.expenses", Asset: "hq.assets", Entry: "hq.calendar"}[model],
        gathered=True,
    )


# The rules this module raises, beside what each counts.
RULES: tuple[FindingRule, ...] = (
    _backlog(
        CONTENT_DRAFTS,
        "Unpublished drafts",
        _Backlog(
            one="draft is not published",
            many="drafts are not published",
            counts="Drafts",
            opens_as="Open drafts",
            route="content:list",
            query={"status": "draft"},
            count=lambda: sections.content_reading()["drafts"],
            notice=True,
        ),
        domain="hq.content",
        operator_action="Finish each draft and publish it, or delete it.",
        no_help_reason="Only a person decides when a piece is ready.",
        bar="a draft is not published, which is work in hand rather than a problem",
    ),
    _backlog(
        CONTENT_UNDOCUMENTED,
        "Published with no doc",
        _Backlog(
            one="published piece has no doc linked",
            many="published pieces have no doc linked",
            counts="Published with no doc",
            opens_as="Link a doc",
            route="content:list",
            query={"no_docs": "1"},
            # Published only: a draft already has its own finding and is too
            # early to be missing documentation.
            count=lambda: (
                ContentItem.objects.filter(status=ContentItem.Status.PUBLISHED)
                .annotate(doc_count=Count("related_documentation"))
                .filter(doc_count=0)
                .count()
            ),
        ),
        domain="hq.content",
        operator_action="Link each published piece to the doc that records what it covers.",
        no_help_reason="Only a person knows which doc a piece belongs with.",
        bar="a published piece has no doc linked, so nothing records what it covers",
    ),
    _backlog(
        CONTACTS_UNREAD,
        "Unread messages",
        _Backlog(
            one="unread message from the contact form",
            many="unread messages from the contact form",
            counts="Unread",
            opens_as="Read messages",
            route="contacts:list",
            query={"status": "unread"},
            count=_unread,
        ),
        domain="hq.contacts",
        operator_action="Read each message, and reply where one asks for it.",
        no_help_reason="A reply is a person's to write.",
        bar="a message from the contact form is unread, and each one waits on a reply",
    ),
    _backlog(
        EXPENSES_WITHOUT_RECEIPTS,
        "Expenses with no receipt",
        _Backlog(
            one="expense has no receipt",
            many="expenses have no receipt",
            counts="With no receipt",
            opens_as="Attach receipts",
            route="expenses:list",
            query={"no_receipts": "1"},
            count=lambda: Expense.objects.filter(receipts__isnull=True).distinct().count(),
        ),
        domain="hq.expenses",
        operator_action="Upload the receipt for each expense, or attach one already in HQ.",
        no_help_reason="A receipt is a file only a person can hand HQ.",
        bar="an expense has no receipt, and a deduction needs its receipt",
    ),
    _links_to_nothing("expense-links-to-nothing", Expense),
    _backlog(
        RECEIPTS_UNLINKED,
        "Receipts attached to nothing",
        _Backlog(
            one="receipt is not attached to an expense or asset",
            many="receipts are not attached to an expense or asset",
            counts="Attached to nothing",
            opens_as="Attach receipts",
            route="receipts:list",
            query={"unlinked": "1"},
            count=lambda: Receipt.objects.filter(related_expense__isnull=True, related_asset__isnull=True).count(),
        ),
        domain="hq.receipts",
        operator_action="Attach each receipt to the expense or asset it proves.",
        no_help_reason="Only a person knows what a receipt was for.",
        bar="a receipt is attached to nothing, so no expense counts it as proof",
    ),
    _backlog(
        ASSETS_MISSING_PURCHASE,
        "Assets with no purchase date or cost",
        _Backlog(
            one="asset has no purchase date or cost",
            many="assets have no purchase date or cost",
            counts="With no purchase date or cost",
            opens_as="Fill in details",
            route="assets:list",
            query={"missing_purchase": "1"},
            named=_unpriced,
            said="Depreciation cannot be calculated without both.",
        ),
        domain="hq.assets",
        operator_action="Fill in each asset's purchase date and cost.",
        no_help_reason="The purchase date and cost are on a receipt only a person has.",
        bar="an active asset has no purchase date or cost, so its depreciation cannot be worked out",
    ),
    _links_to_nothing("asset-links-to-nothing", Asset),
    _links_to_nothing("event-links-to-nothing", Entry),
)
