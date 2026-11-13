"""What a card lists, as data: figures, readings, rows and dated events.

Each is the shape one shared partial draws (``_kpi_grid.html``,
``_insight_grid.html``, ``_record_list.html``, ``_timeline.html``), which
``hq_sdk.ui`` exports so an extension fills the same part the host does.
"""

from dataclasses import field
from datetime import date, datetime
from typing import Protocol

from .display_words import QUOTED, display, in_words
from .drawings import AllocationBar, Dots, Trend
from .workflow_contracts import ActionLink, WorkflowPlan
lazy from .ui import STATUS_VALUES, _one_of


@display
class Kpi:
    """One figure: what it counts, its value, and a line saying more.

    The shape ``partials/_kpi_grid.html`` draws, alone or as one of a row.
    """

    label: str
    value: str | int
    detail: str = ""
    url: str = ""
    is_zero: bool = False
    # A mark before the label, by its name in ``partials/_icon.html``: what
    # kind of thing the figure counts, for an eye scanning a card of several.
    # A name the set does not draw shows nothing.
    icon: str = ""
    # What the figure looks like beside its value: ``Dots`` for a part of a
    # whole, ``Trend`` for a direction, or under it an ``AllocationBar`` with
    # its legend for the parts the whole is made of. One or none. It repeats
    # what the value and the note already say, so the words stay.
    drawing: Dots | Trend | AllocationBar | None = None
    # Which way a figure that is itself a change went, as a status tone: the
    # value is drawn in it, in a featured row too, so a rise and a fall never
    # read alike.
    tone: str = ""
    # The same for the line under the value, where the value is a level and
    # the line says how it moved ("+$120 in 30 days").
    detail_tone: str = ""
    # Where the line under the value leads, when it counts things with a page
    # of their own ("12 expenses").
    detail_url: str = ""

    def __post_init__(self) -> None:
        _one_of("Kpi tone", self.tone, STATUS_VALUES, or_empty=True)
        _one_of("Kpi detail_tone", self.detail_tone, STATUS_VALUES, or_empty=True)
        if self.detail_url and not self.detail:
            raise ValueError("Kpi detail_url needs a detail: a link is said by its words.")

    @property
    def holds_links(self) -> bool:
        """Whether a link stands inside the figure: a dot or a legend row that
        leads on, or its line under the value. A link does not hold links, so
        such a figure's page is reached through its value instead of the whole
        cell."""

        return bool(self.detail_url or (self.drawing is not None and self.drawing.links))


@display
class Insight:
    """A reading, what it means, and the next action.

    The shape extensions emit for ``partials/_insight_grid.html``. Provided by
    the host so a surface needing decision support does not restate the card
    markup or re-map its own status names onto styling.
    """

    status: str
    eyebrow: str
    title: str
    value: str
    body: str
    action: str = ""
    url: str = ""
    # How many things this insight stands for, when it stands for a countable
    # backlog. ``value`` is a display string and may be a balance or a distance,
    # so a surface that totals a queue cannot infer a quantity from it. Left
    # unset, the insight counts as the single thing it describes, which is
    # what an alert is.
    magnitude: int | None = None
    # Optional closure loop emitted by any host or plugin domain. The queue and
    # insight partials render it generically; the domain still owns every action.
    workflow: WorkflowPlan | None = None
    # Quick actions offered on the item itself, each going through its owner's
    # route exactly as the owner's own page would.
    actions: tuple[ActionLink, ...] = ()
    # What this item is about, stable for as long as it is the same matter:
    # "finding:<rule>:<subject>", "approval:<id>". Read state follows it, so it
    # must not carry anything that changes while the matter stays the same: a
    # count, a days-left figure, a message. Unique within its source. Left empty,
    # the eyebrow and title stand in for it.
    key: str = ""
    # The thing this item is about, as a link to its own page.
    subject: ActionLink | None = None
    # The one thing on its subject it is about, where the subject holds many:
    # a container's name on its machine. A page about one of them alone tells
    # its own problems from its neighbors' by it.
    part: str = ""
    # A notice reports something that happened or was measured; there is
    # nothing to resolve and no reading to refresh until it clears. The queue
    # keeps notices apart from what needs doing and out of that count, and a
    # reader who dismisses one is done with it: it returns only when what it
    # reports (its ``body``) changes. So a notice's body says the occurrence
    # (the run, the day, the figures), and a counter that moves on its own
    # (days since) belongs in ``value``.
    notice: bool = False
    # What kind of matter this is, as its owner would head a list of them:
    # "Image advisories", "Tailscale updates". Several open items of one family
    # from one source are folded under that heading in the queue, so a dozen of
    # the same thing read as one line until opened. Left empty, the item
    # stands alone.
    family: str = ""
    # When this began, where its owner knows: the moment a condition was first
    # seen, a request was made, a key ran out. A card says "Since Oct 3".
    # Left unset when the owner knows only that it holds now: a card that
    # guesses a start is worse than one that gives none.
    since: datetime | None = None
    # When the reading this item is drawn from was taken, where it is drawn
    # from one: a card says how old what it claims is.
    observed_at: datetime | None = None
    # What was read that shows it, as (label, value) pairs: "Device address",
    # "192.0.2.10". A card draws each label once with its values beside it.
    facts: tuple[tuple[str, str], ...] = ()
    # Pages that help look into it (where it sits, what depends on it), drawn
    # as one line of links under its buttons. Never something that acts.
    links: tuple[ActionLink, ...] = ()
    # The capabilities a reader must hold to be shown it, for an item drawn
    # from what only they may read. Empty, every reader of the queue sees it.
    read_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _one_of("Insight status", self.status, STATUS_VALUES)
        for label, _value in self.facts:
            in_words(label, label=True)


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
    status: str = ""
    badge: str = ""
    # When the record happened, after ``meta``: a value, so the row says it
    # in HQ's time words as the day it fell on, the moment itself on hover.
    at: date | None = None
    # What can be done to the record from its row, last on the line, each a
    # ``PageAction``: a link to a page, or a POST through ``post_button`` ("Move up", "Delete"). A
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
        _one_of("ListRow status", self.status, STATUS_VALUES, or_empty=True)
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
    status: str = "neutral"
    badge: str = ""

    def __post_init__(self) -> None:
        _one_of("TimelineItem status", self.status, STATUS_VALUES)


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
