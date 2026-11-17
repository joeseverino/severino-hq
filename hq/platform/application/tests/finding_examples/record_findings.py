from collections.abc import Callable
from datetime import date
from decimal import Decimal
from unittest import mock

from hq.domains.assets.models import Asset
from hq.domains.calendars.models import Entry
from hq.domains.content.models import ContentItem
from hq.domains.receipts.models import Receipt

from ..test_references import an_expense
from . import Example, Examples

PRICED = {"purchase_date": date(2026, 3, 1), "total_cost": Decimal("120.00")}


def held(*rows: Callable[[], object], raises: tuple[str, ...] = ()) -> Example:
    """HQ holding the records each of ``rows`` writes, read inside what the last returns."""

    def given() -> object:
        return [row() for row in rows][-1] if rows else None

    return Example(given=given, raises=raises)


def unread(count: int) -> Callable[[], object]:
    """The contact form's store, as its last read left it."""

    return lambda: mock.patch("hq.domains.contacts.inbox.unread", return_value=(count, "ok"))


def piece(status: str) -> Callable[[], object]:
    return lambda: ContentItem.objects.create(title=f"A {status} piece", status=status)


def asset(name: str, **fields) -> Callable[[], object]:
    return lambda: Asset.objects.create(item_name=name, **fields)


def receipt(**links) -> Callable[[], object]:
    return lambda: Receipt.objects.create(file="receipts/example.pdf", **links)


def attached() -> Receipt:
    """A receipt that proves an expense."""

    return Receipt.objects.create(file="receipts/example.pdf", related_expense=an_expense())


EXAMPLES = (
    Examples(
        "content-drafts",
        fires=(held(piece("draft"), piece("draft"), raises=("2 drafts are not published",)),),
        quiet=(held(piece("published")),),
    ),
    Examples(
        "content-undocumented",
        # A draft is too early to be missing its doc.
        fires=(held(piece("published"), raises=("1 published piece has no doc linked",)),),
        quiet=(held(piece("draft")),),
    ),
    Examples(
        "contacts-unread",
        fires=(held(unread(2), raises=("2 unread messages from the contact form",)),),
        quiet=(held(unread(0)),),
    ),
    Examples(
        "expenses-without-receipts",
        fires=(held(an_expense, raises=("1 expense has no receipt",)),),
        quiet=(held(attached),),
    ),
    Examples(
        "expense-links-to-nothing",
        fires=(
            held(
                lambda: an_expense(about="asset:gone"),
                raises=("Example Registrar\xa0· Renewal links to asset:gone, which HQ does not have",),
            ),
        ),
        quiet=(held(asset("A router", slug="a-router"), lambda: an_expense(about="asset:a-router")),),
    ),
    Examples(
        "receipts-unlinked",
        fires=(held(receipt(), raises=("1 receipt is not attached to an expense or asset",)),),
        quiet=(held(attached),),
    ),
    Examples(
        "assets-missing-purchase",
        fires=(held(asset("A router"), asset("A switch", **PRICED), raises=("1 asset has no purchase date or cost",)),),
        quiet=(held(asset("A switch", **PRICED)),),
    ),
    Examples(
        "asset-links-to-nothing",
        fires=(
            held(
                asset("A server", infrastructure="machine:gone"),
                raises=("A server links to machine:gone, which HQ does not have",),
            ),
        ),
        quiet=(held(asset("A server")),),
    ),
    Examples(
        "event-links-to-nothing",
        fires=(
            held(
                lambda: Entry.objects.create(title="Check", starts_on=date(2026, 3, 1), about="asset:gone"),
                raises=("Check links to asset:gone, which HQ does not have",),
            ),
        ),
        quiet=(held(lambda: Entry.objects.create(title="Check", starts_on=date(2026, 3, 1))),),
    ),
)
