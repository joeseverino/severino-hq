"""What a card lists, as data: figures, readings, rows and dated events.

Each is the shape one shared part draws (a band of facts,
``_insight_grid.html``, ``_record_list.html``, ``_timeline.html``), which
``hq_sdk.ui`` exports so an extension fills the same part the host does.
"""

from dataclasses import field
from datetime import date
from typing import Literal, Protocol

from .display_words import QUOTED, display
from .drawings import AllocationBar, Dots, Trend
from .tone import Tone


@display
class Kpi:
    """One figure: what it counts, its value, and a line saying more.

    The shape a band of facts draws as one of its facts (``{% facts
    items=... %}``).
    """

    label: str
    value: str | int
    detail: str = ""
    url: str = ""
    is_zero: bool = False
    # A mark before the label, by its symbol in ``static/img/icons.svg``: what
    # kind of thing the figure counts, for an eye scanning a card of several.
    # A name the set does not draw shows nothing.
    icon: str = ""
    # What the figure looks like beside its value: ``Dots`` for a part of a
    # whole, ``Trend`` for a direction, or under it an ``AllocationBar`` with
    # its legend for the parts the whole is made of. One or none. It repeats
    # what the value and the note already say, so the words stay.
    drawing: Dots | Trend | AllocationBar | None = None
    # Which way a figure that is itself a change went, as a status tone: the
    # value is drawn in it at any size, so a rise and a fall never
    # read alike.
    tone: Tone | Literal[""] = ""
    # The same for the line under the value, where the value is a level and
    # the line says how it moved ("+$120 in 30 days").
    detail_tone: Tone | Literal[""] = ""
    # Where the line under the value leads, when it counts things with a page
    # of their own ("12 expenses").
    detail_url: str = ""
    # What pointing at the figure says, its whole sentence, where the cell
    # holds only a word or two of it (an hour's forecast).
    tip: str = ""

    def __post_init__(self) -> None:
        if self.detail_url and not self.detail:
            raise ValueError("Kpi detail_url needs a detail: a link is said by its words.")


@display
class Insight:
    """A reading, what it means, and the next action: the card
    ``partials/_insight_grid.html`` draws, every field of it.

    Provided by the host so a surface needing decision support does not
    restate the card markup or re-map its own status names onto styling. What
    needs a person goes on the queue as a ``Finding``.
    """

    status: Tone
    eyebrow: str
    title: str
    value: str
    body: str
    action: str = ""
    url: str = ""


class RowLink(Protocol):
    """What a row reads of the thing it names: an ``EntityLink`` fills it.

    Named by its shape, as ``RowAction`` is, so a card's items stay clear of
    the link builder and what it reads.
    """

    @property
    def label(self) -> str:
        """The words the link is drawn with."""

    @property
    def url(self) -> str:
        """Where it leads."""

    @property
    def external(self) -> bool:
        """Whether it leads out of HQ."""

    @property
    def kind_label(self) -> str:
        """What kind of thing it leads to, in words."""


class RowAction(Protocol):
    """What a row reads of an action it ends with: a ``PageAction`` fills it.

    Named by its shape rather than imported, so a card's items stay clear of
    the page machinery behind ``pages``.
    """

    @property
    def label(self) -> str:
        """The words its button is drawn with."""

    @property
    def url(self) -> str:
        """Where it sends."""

    @property
    def method(self) -> str:
        """The HTTP method it sends with."""

    @property
    def disabled(self) -> bool:
        """Whether it is drawn but cannot be pressed."""


@display
class ListRow:
    """One line of a compact record list: what it is, and when.

    The shape ``partials/_record_list.html`` renders, so history panels, queues
    and "latest N" lists read the same everywhere and a change to the row lands
    everywhere at once. A row is for records that speak for themselves; one
    that needs interpreting belongs in ``_insight_grid.html``.
    """

    title: str = ""
    # Inline after the title, muted: the one fact that distinguishes this row.
    detail: str = ""
    # Trailing, right-aligned, in words: a duration, a measure, "Due Oct 3".
    # Kept short: it is scanned.
    meta: str = ""
    url: str = ""
    # Leaves HQ. Rendered so the operator knows before they click, and so a
    # linked page cannot reach back through window.opener.
    external: bool = False
    # Optional state. `badge` carries the text, so state is never color alone.
    status: Tone | Literal[""] = ""
    badge: str = ""
    # When the record happened, after ``meta``: a value, so the row says it
    # in HQ's time words as the day it fell on, the moment itself on hover.
    at: date | None = None
    # What can be done to the record from its row, last on the line, each a
    # ``PageAction``: a link to a page, or a POST through ``button`` ("Move up", "Delete"). A
    # glyph's ``title`` names it aloud.
    actions: tuple[RowAction, ...] = ()
    # The thing the row names, in place of ``title`` and ``url``: drawn as
    # every mention of an entity is, so the name opens its page or, on that
    # page, stands as plain text.
    link: RowLink | None = None
    # A project's category key ("homelab"), drawn as its color before the
    # title; ``detail`` says it in words, since color is never alone.
    category: str = ""
    # Where a search found the record, under its line: the record's own text
    # in runs, each with whether it is the match, which is marked. Quoted as
    # the record holds it, so it is not held to words.
    snippet: tuple[tuple[str, bool], ...] = field(default=(), metadata=QUOTED)
    # The words a place is known by besides its name, each a chip after its
    # line: a machine's state, the abilities of a connection a search matched.
    chips: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.link is not None and (self.title or self.url or self.external):
            raise ValueError("A ListRow names its record once: by its title and url, or by its link.")
        if self.status and not self.badge:
            raise ValueError("ListRow status needs a badge: state is never carried by color alone.")


@display
class TimelineItem:
    """One dated event in the host-owned planning horizon.

    Plugins emit meaning; HQ owns chronology, accessibility, responsive layout,
    and state styling. The date remains a real ``date`` until the template so
    callers cannot accidentally sort display strings such as "Nov 1".
    """

    when: date
    title: str
    detail: str = ""
    url: str = ""
    status: Tone = Tone.NEUTRAL
    badge: str = ""


@display
class Timeline:
    """A chronological, dependency-free planning surface."""

    title: str
    description: str
    items: tuple[TimelineItem, ...]

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.items, key=lambda item: (item.when, item.title)))
        if ordered != self.items:
            raise ValueError("Timeline items must be sorted chronologically.")
