"""Facts: every label and its value a page shows, drawn by one part, its
``{% facts %}`` around each ``{% fact %}``."""

from collections.abc import Iterable
from typing import NamedTuple

from django import template
from django.template.base import token_kwargs
from django.utils.text import capfirst

from hq.platform.application.card_items import Answer
from hq.platform.application.display_words import Code
from hq.platform.application.page_parts import html_attributes
from hq.platform.core.part_examples import facts
from hq.platform.core.template_parts import Option, Part, SlotsNode

register = template.Library()

FACTS_PARTIAL = "partials/_facts.html"
FACT_PARTIAL = "partials/_fact.html"

# Each layout, drawn as its class (``facts-band``) by the partial.
LAYOUTS = ("list", "band", "inline")
# How large a band sets its values: a strip of small figures read across
# (``small``), a page's status facts, its headline figures (``large``), or
# headline figures led by the first (``lead``).
SIZES = ("small", "regular", "large", "lead")


def _attributes(_name: str, given: dict) -> dict:
    """What a page gave, each ``data_*`` option as an attribute of the element."""

    data = {option.replace("_", "-"): value for option, value in given.items() if option.startswith("data_")}
    return {
        **{option: value for option, value in given.items() if not option.startswith("data_")},
        "attributes": html_attributes(data),
    }


class FactTakes(Part):
    """What ``{% fact %}`` takes: its label first, then its options. Given
    ``value``, the option ``bare_with`` names, it encloses nothing."""

    def values(self, given: dict) -> dict:
        given = super().values(given)
        if isinstance(answer := given.get("value"), Answer):
            if given.get("detail"):
                raise ValueError("A fact given an Answer takes its detail from the answer's `because`.")
            given = {**given, "value": answer.short, "detail": answer.because, "tone": given["tone"] or answer.tone}
        detail = given.get("detail")
        if isinstance(detail, list | tuple):
            return {**given, "detail": "", "lines": _lines(line for line in detail if line)}
        return given


class Line(NamedTuple):
    """One line of a fact's detail as its partial draws it: words, code, or
    the links that stand together, a dotted run of names."""

    text: str = ""
    code: bool = False
    links: tuple = ()


def _lines(given: Iterable[object]) -> tuple[Line, ...]:
    """Each line as drawn, words after a finished sentence raised to start
    their own."""

    drawn: list[Line] = []
    for line in given:
        if isinstance(line, Code):
            drawn.append(Line(line, code=True))
        elif isinstance(line, str):
            follows = bool(drawn) and drawn[-1].text.rstrip().endswith((".", "!", "?"))
            drawn.append(Line(capfirst(line) if follows else line))
        elif drawn and drawn[-1].links:
            drawn[-1] = drawn[-1]._replace(links=(*drawn[-1].links, line))
        else:
            drawn.append(Line(links=(line,)))
    return tuple(drawn)


FACTS = Part(
    name="facts",
    rules="""
    A band of facts (`layout="band"`) is every run of small labeled figures: a page's status facts,
    its headline figures, a forecast's hours, the dashboard's glance. It is the few facts that sum up
    a page or a part, each a cell with its label and mark over its value over its detail. Its cells
    are sized by what they hold: a wrapping row where each cell starts at its own widest line, a
    row's cells share the room left over, and a cell takes the next row only when the band has no
    room for it. So a label wraps only in a band too narrow for it on one line. A band holding an
    identifier (a version, a tag) counts its rows on a floor as wide as one, so a short version
    stands whole on its line (*Gate:* `split identifiers`). Each cell is its own
    stack, top-aligned, its value right under its label, no taller than what it holds; a ragged line
    across cells is better than a gap inside one.

    A band draws no box of its own: a box is the card around it, so a band outside a card draws none.
    Anywhere but a card holding it alone (a section's body, a card holding more, the page's ground)
    the facts stand apart by a gap (`--band-gap`) with no edges (`--band-rules`), their words at the
    content's edge.

    `size` sets the values: `small`, an even grid of small centered figures read across, as a
    forecast's hours are, a note under one marked; `regular`, status facts; `large`, the headline
    figures a page is read for; `lead`, headline figures led by the first, which stands larger and on
    a row of its own where the band has no room for it beside them. Given `items` (each a `Kpi`), the
    band draws each as its fact. Under 24rem a band reads as a list, each fact a row of its own, its
    value beside its label; a value holding an identifier stays there only while it fits whole, and
    takes a line of its own under the label where it does not. Small figures keep their grid, and headline figures (`large`, `lead`) and a band beside a page's title
    their counted rows, two to a row (a phone's head: label beside value, over a note whose run wraps
    whole parts to the lines it needs). Headline figures stand on a wider floor, so three take a row
    only once the band is about 360px wide, each value sized to its cell, never under the band's.

    **A band's values are one size.** A domain's overview is a band of facts at `size="large"`, every
    figure the same size, in its contributor's order. Values, context and links render once from the
    `Kpi` collection. Overview cards fit their available width, placing three domains side by side
    when space permits; empty contributors take no space.

    A band of facts beside a page's title (the dashboard's glance) stands on the page's ground: beside
    the title while its facts fit there, and on a line of its own under it, from the page's edge,
    where not. Fewer than four readings take a column each as wide as four would, so two stand
    together rather than at the far ends of the head. *Gate:* `readings apart` in the survey.

    A list, the default, is the facts about one thing, each label beside its value. It lays as many
    pairs to a row as keep each a short label and a short value wide, so a few short facts fill a
    wide frame rather than stacking down its start. The pairs stand in columns every row shares, so
    each label stands under the label above it, and a list of fewer pairs than its columns spreads
    them over the row; every label is one width, so the values of a column line up. A value that is
    a run of several (a `labeled` or `codes` list, addresses) takes the row. Narrower than two pairs
    it is one pair to a row. A
    label with several values holds them as a list inside one fact. An identifier in a value (code,
    a `|breakable` name) runs inline, code a chip on each line it wraps to, so a value's first line
    stands on its label's line in every engine; the value keeps a chip's end padding free at its end,
    so a wrapped line's chip stays inside its column in any face. A list opened to read more (in a disclosure, a
    popover, a dialog) or set above what a form changes keeps one pair to a row, sits closer and
    ends with what holds it. A table row's small print keeps one pair to a row too. Where it stands
    decides this, never an option, so the same summary drawn on a page and in a dialog takes each
    one's spacing.

    A line (`layout="inline"`) is a card's summary, the counts across a domain or the whole of HQ:
    each fact one phrase, its bold value then its words ("3 rules"), agreeing with it, then a dot
    and its detail ("1 Draft · 22 published"), a fixed gap apart, wrapping fact by fact, so a wide
    card holds them on one line. Under the card's head it takes a rule above.

    **A frame is filled.** A list of facts lays its pairs across its frame. A card of only a pair or
    two shares a row with the sections beside it, so its values never end a third of the way across
    the page. A record page's facts are a `{% card %}` that opens its `{% layout %}`, beside its
    notes and related records. In a flow, a frame of facts grows into its row as every member does.
    """,
    classes=("facts-band", "facts-inline", "facts-list", "is-large", "is-lead", "is-small"),
    partial=FACTS_PARTIAL,
    about=(
        "Every label and its value a page shows, each a `{% fact %}`; given `items=` it draws each item "
        "as a fact and encloses nothing."
    ),
    options={
        "layout": Option(
            default="list",
            choices=LAYOUTS,
            about=(
                "A list, each label beside its value; a band, the few facts that sum up a page, each a cell; "
                "a line (`inline`), a card's summary, each fact its value then its words."
            ),
        ),
        "label": Option(about="What the whole is, for a reader, when nothing on the page heads it."),
        "size": Option(
            default="regular",
            choices=SIZES,
            about=(
                "How large a band sets its values: small figures read across, status facts, the headline "
                "figures a page is read for, or headline figures led by the first."
            ),
        ),
        "items": Option(about="Figures (`Kpi`) to draw as its facts, which makes it one tag with no end."),
    },
    data=True,
    bare_with="items",
    shape=_attributes,
    public=True,
    example='{% facts layout="band" %}{% fact "Running" value="3" url="/example/" %}{% endfacts %}',
    states=facts.FACTS_STATES,
)
FACT = FactTakes(
    name="fact",
    rules="""
    Every label and its value is a fact: `{% fact "Label" value=n url=... detail=... %}` inside `{%
    facts %}`, or the block form `{% fact "Label" %}markup{% detail %}lines{% endfact %}` for a value
    that is markup. A zero and a linked count stand alike. A `Kpi` or a card that counts things
    takes a `Counted` as its label, which words it to agree with its figure ("1 Draft", "2 Drafts").
    A fact is drawn as a figure is: its label
    with its mark before it, its value large under it, its detail under that. What a figure counts may
    follow it in a `<small>` ("9 containers"), in the label's voice. A link in a value is the value's
    ink, underlined only under the pointer and the focus, its fact tinted, so only a toned fact's value
    is colored. A fact whose one target is its value's link is that link whole, its label and detail
    pressed as its value is; one holding another target keeps each its own. Given `{% heading %}`,
    `{% panel %}` and `{% foot %}` parts and an `id`, its value is the button that opens the panel
    in place, as a menu does, and each `data_*` option is its attribute, so a fragment region in the
    band holds facts refreshed in place as its own (`core/_dashboard_glance.html`).

    **A value is the short answer.** A state, a count, a version: never a sentence, never its label's
    or its state's word again ("Waiting: Waiting for…"). The sentence that explains it is the detail,
    and a tone other than neutral or good says why there. A value a domain object words is an
    `Answer` (`value=update.brings_said`): its `short` is the value, its `because` the detail's lines
    (words, a link, or `Code`), its `tone` the fact's. So a sentence has nowhere to go but the
    detail: an `Answer` whose short is a sentence, or whose attention or serious tone has no reason,
    is refused where it is built, and a reason that opens with the short answer again is dropped.
    A measure in the detail says what it measures ("asked 3 minutes ago", never a bare "3 s").

    A fact's detail is running words. Its lines are the ones its markup declares: a `<small>` each,
    or a `detail=` given as a list. What a fact holds past its lines (its record list, a disclosure
    of what runs now) follows them in the detail, so a page's summary cards are a band of facts too,
    never a grid of its own. A line naming something longer than its cell (an image's path) is its
    `cut`, the detail's first line, held to one line and cut at its end, so its cell is no taller than
    its neighbors, and its tip reads it whole (`cut_whole`, where that is more than the line). A fact drawn by hand (a page's own band) writes the part's classes, which
    no page may, and a figure set bold apart from the words that count it is refused by the
    `split-count` shape of `hand_drawn.SHAPES`.

    **A figure may be drawn as well as said.** `Kpi` (and `{% fact %}`) takes one `drawing`, or none,
    drawn beside its value or under its detail:

    - `Dots`, a part of a whole, one dot each. `Dots.of(filled, whole)` when the parts are only a
      count; `Dots((Dot(...), ...))` when each is something with a page of its own, which then names
      itself when pointed at and links there, beside the figure's own link on its value. Past six,
      dots stand in even rows at most four wide. Where the column has no room for them beside the
      value they take the line under it, so a figure's dots never reach the figure beside it (the
      survey's `into a neighbor`). Past a dozen, dots are a texture and are not drawn. A history is
      `Dots(..., ordered=True)`, drawn in its own order, oldest first, as the HQ page draws a piece of
      scheduled work's recent runs, each linking to its job. `partials/_dots.html` draws either,
      wherever it stands.
    - `Trend`, recent readings oldest first, drawn as a line where the card has room, each reading
      saying what it is when pointed at.
    - `AllocationBar`, the parts of a whole, drawn under the figure and its line as a bar with its
      legend set one under another (`partials/_allocation.html`), each part linking where it has a
      page. In a card the line and the parts share the line's row, so the figures beside it keep their
      lines level.

    and an `icon`, a mark from `partials/_icon.html` before the label, on figures only: headings
    carry none. A drawing repeats what the value and its note already say, so the words stay, and one
    that is only decoration is not announced. Nothing is drawn against a measure the domain does not
    have: a date that runs out shows the date, not a bar across an invented span.

    A figure that is itself a change takes `tone`, a status tone its value is drawn in, at any size,
    so a rise and a fall never read alike. A figure that is a level says how it moved in its line,
    which takes `detail_tone` the same way. `detail_url` links the line where it counts things with a
    page of their own ("12 expenses"). It needs a `detail`, since a link is said by its words.
    """,
    classes=(
        *("band-icon", "fact", "fact-cut", "fact-detail", "fact-figure", "fact-label", "fact-open", "fact-panel"),
        *("fact-panel-foot", "fact-panel-head", "fact-shown", "fact-value", "is-zero", "tone-*"),
    ),
    partial=FACT_PARTIAL,
    about=(
        "One fact of a `{% facts %}`: its label, its value and the detail under it. Given `value=` it encloses "
        "nothing; a value in markup is its block, its detail after `{% detail %}`, and a panel its value "
        "opens in place after `{% heading %}`, `{% panel %}` and `{% foot %}`."
    ),
    parts=("value", "detail", "heading", "panel", "foot"),
    ordered={"label": Option(required=True, about="Its label.")},
    options={
        "value": Option(
            about=(
                "Its value, which makes it one tag with no end; an `Answer` gives its short answer as the value, "
                "its reasons as the detail's lines and its tone."
            )
        ),
        "url": Option(about="The page in HQ its value links to; an outside one is the block form's `{% entity %}`."),
        "modal": Option(about="The dialog its value's link opens where script runs."),
        "id": Option(about="The id of the panel its value opens in place, given a `panel` part."),
        "title": Option(about="What pointing at it says."),
        "detail": Option(about="The words under the value, or a list of them, a line each."),
        "detail_url": Option(about="The page the words under the value lead to."),
        "detail_tone": Option(about="A status tone the words under the value are colored by."),
        "cut": Option(
            about="A line longer than its cell, the detail's first, cut at its end and read whole from its tip."
        ),
        "cut_whole": Option(
            about="What the cut line's tip reads, where that is more than the line; the line by default."
        ),
        "tone": Option(about="A status tone the fact is colored by, for what it means."),
        "zero": Option(about="Its value is nothing counted, drawn quiet."),
        "icon": Option(about="The icon beside it."),
        "drawing": Option(
            about=(
                "What its value looks like, beside it: `Dots` for a part of a whole, `Trend` for a direction, "
                "or under it an `AllocationBar` with its legend."
            )
        ),
    },
    data=True,
    bare_with="value",
    shape=_attributes,
    public=True,
    example=(
        '{% facts %}{% fact "Version" icon="layers" %}<code>1.4.2</code>{% detail %}Tag only{% endfact %}{% endfacts %}'
    ),
)


def _options(part: Part, bits: list[str], parser) -> dict:
    given = token_kwargs(list(bits), parser)
    if len(given) != len(bits) or not part.admits(set(given)):
        raise template.TemplateSyntaxError(part.refused())
    return given


@register.tag(name="facts")
def compile_facts(parser, token) -> SlotsNode:
    given = _options(FACTS, token.split_contents()[1:], parser)
    if FACTS.bare_with in given:
        return SlotsNode(FACTS, {}, given)
    content = parser.parse(("endfacts",))
    parser.delete_first_token()
    return SlotsNode(FACTS, {"content": content}, given)


@register.tag(name="fact")
def compile_fact(parser, token) -> SlotsNode:
    bits = token.split_contents()[1:]
    if not bits or "=" in bits[0]:
        raise template.TemplateSyntaxError('fact takes its label first: {% fact "Label" value=... %}')
    given = {"label": parser.compile_filter(bits[0]), **_options(FACT, bits[1:], parser)}
    if FACT.bare_with in given:
        return SlotsNode(FACT, {}, given)
    found = {name: template.NodeList() for name in FACT.parts}
    ahead = list(FACT.parts)
    while True:
        found[ahead.pop(0)] = parser.parse((*ahead, "endfact"))
        marker = parser.next_token().contents
        if marker == "endfact":
            return SlotsNode(FACT, found, given)
        ahead = ahead[ahead.index(marker) :]


# The template contract reads a tag's parameters from what it wraps.
compile_facts.__wrapped__ = FACTS  # type: ignore[attr-defined]  # a function's attribute, which typeshed does not declare
compile_fact.__wrapped__ = FACT  # type: ignore[attr-defined]  # as above
