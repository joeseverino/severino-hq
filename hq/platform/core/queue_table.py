"""The queue as a declared table (``WORK_QUEUE``'s rules): a row per item,
its family folded under a row of its own, each said once by its decision
(``application.decisions``)."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from django.utils.text import slugify

from hq.platform.application import action_items
from hq.platform.application.decisions import decision
from hq.platform.application.tables import Below, Cell, DataTable, Group, Piece, Row, StatusMark, TableColumn
from hq.platform.application.tone import Tone
from hq.platform.core.included_queue import ICON_LINKS, INSIGHT_FACTS, OPERATOR_STEPS, ORIGIN_PANEL
from hq.platform.core.templatetags.action_tags import remedies, triage
from hq.platform.core.templatetags.status_tags import origin_tip, urgency_tone

_COLUMNS = (TableColumn("Item"),)
# A queue's rows are the rest of what a part says, each sizing its own controls.
_QUEUE: dict[str, Any] = {"headed": False, "data": {"line": "flat"}, "controls_per_row": True}


@dataclass(frozen=True, slots=True)
class Where:
    """Where the queue is drawn: under a domain's own heading (``grouped``),
    on the page of the thing it is about (``here``), where the reader sorts
    their own queue (``triage``), away from the queue (``glance``), and, on
    a page holding several, the id its families' rows keep apart (``prefix``)
    with the address that dismisses a family whole (``aside_url``)."""

    grouped: bool = False
    here: bool = False
    triage: bool = False
    glance: bool = False
    prefix: str = ""
    aside_url: str = ""


def table(context: Any, items: Iterable[Mapping[str, Any]], where: Where, *, fold: bool = False) -> DataTable:
    """The queue drawn whole: its entries, a family folded under its name when ``fold``."""

    found = action_items.families(list(items), fold=fold)
    return DataTable("Queue", _COLUMNS, tuple(_entries(context, found, where)), **_QUEUE)


def by_domain(context: Any, groups: Iterable[Any], where: Where, *, titled: bool = True) -> DataTable:
    """The queue under its domains (``action_items.by_source``), a group
    each, headed by its domain unless the card around it is."""

    bodies = tuple(
        Group(
            group["label"] if titled else "",
            tuple(_entries(context, group["entries"], where)),
            count=len(group["items"]),
            href=group.get("url") or "",
        )
        for group in groups
    )
    return DataTable("Queue", _COLUMNS, groups=bodies, **_QUEUE)


def _entries(context: Any, entries: Iterable[Mapping[str, Any]], where: Where) -> Iterable[Row]:
    for entry in entries:
        if not entry["folded"]:
            yield from _rows(context, entry["items"], where)
            continue
        family = slugify(f"{where.prefix}-{entry['id']}")
        away = triage(family=entry["id"], url=where.aside_url) if where.aside_url else triage(sorts=False)
        yield _family(context, entry, family, away)
        yield from _rows(context, entry["items"], where, folded=family)


def _family(context: Any, entry: Mapping[str, Any], family: str, away: Any) -> Row:
    """A family's row: what kind, how many and how bad, what is inside, and the act its items share."""

    status = entry["status"]
    css = "is-mark" if status == "serious" else "is-mark visually-hidden"
    words = Tone(status).urgency
    mark = Cell(status=StatusMark(words, tone=status, css=css, tip=words))
    gist = Cell(entry["summary"], shows="gist", data={"attention-detail": ""})
    name = Cell(entry["family"], name=True, lead=True, count=len(entry["items"]), fold=True, parts=(mark, gist))
    return Row(
        (name,),
        actions=tuple(remedies(context, entry["lead"])),
        put_away=away,
        folds=family,
        data={"queue-family": ""},
    )


def _rows(context: Any, items: Iterable[Mapping[str, Any]], where: Where, *, folded: str = "") -> Iterable[Row]:
    for entry in items:
        item = decision(entry)
        away = triage(item, sorts=where.triage)
        title = _title(item, where)
        if item.get("aside"):
            yield Row((title,), put_away=away, id=_id(item, where), data={"attention-item": ""}, folded=folded)
            continue
        yield Row(
            (_said(item, title, where),),
            actions=tuple(remedies(context, item["actions"])),
            put_away=away,
            id=_id(item, where),
            data={"attention-item": ""},
            folded=folded,
        )


def _id(item: Mapping[str, Any], where: Where) -> str:
    return item["anchor"] if where.triage else ""


def _title(item: Mapping[str, Any], where: Where) -> Cell:
    """What is wrong, linked to the thing it is about, or to the row's own
    page away from the queue; plain on that thing's own page."""

    url = item["page"] if where.glance else "" if where.here else item["href"]
    return Cell(item["label"], url, role="prose", lead=True, data={"attention-title": ""})


def _said(item: Mapping[str, Any], title: Cell, where: Where) -> Cell:
    """The row's line (its title, its urgency and what it means), what raised
    it, how old its reading is, and what its open line holds."""

    dialog = f"modal-{item['origin']['panel']}"
    quiet = where.glance or item.get("notice") or item["status"] != "serious"
    mark = StatusMark(
        item["urgency"],
        tone=urgency_tone(item),
        css="is-mark visually-hidden" if quiet else "is-mark",
        tip=origin_tip(item, "mark"),
        data_origin=dialog,
    )
    # What raised it opens from its urgency, drawn on its line and never seen there.
    line = [Cell(status=mark), Cell(piece=Piece(ORIGIN_PANEL, {"item": item}, words=""))]
    if item.get("detail"):
        line.insert(1, Cell(item["detail"], shows="gist", data={"attention-detail": ""}))
    said = [] if where.grouped else [Cell(item["source"])]
    if late := item.get("late"):
        said += [Cell.reading(reading=late["reading"]), Cell(late["every"])]
    lines = ()
    if said:
        first, *rest = said
        lines = (replace(first, muted=True, parts=tuple(replace(each, dot=True) for each in rest)),)
    return replace(title, parts=tuple(line), lines=lines, below=_below(item))


def _below(item: Mapping[str, Any]) -> tuple[Below, ...]:
    """What was read, what to do and why it cannot be done here, and the pages that help."""

    lines = []
    if item["facts"]:
        lines.append(Cell(piece=Piece(INSIGHT_FACTS, {"facts": item["facts"]}, words=INSIGHT_FACTS.about)))
    if item["advice"]:
        lines.append(Cell(item["advice"]))
    if item["steps"]:
        lines.append(Cell(piece=Piece(OPERATOR_STEPS, {"steps": item["steps"]}, words=OPERATOR_STEPS.about)))
    if item["reason"]:
        lines.append(Cell(item["reason"], data={"attention-reason": ""}))
    if item.get("links"):
        lines.append(Cell(piece=Piece(ICON_LINKS, {"links": item["links"]}, words=ICON_LINKS.about)))
    return (Below("", tuple(lines)),) if lines else ()
