"""The layout: how a page lays out a run of its sections, each way one kind
of one part, so no page writes a layout container of its own."""

from django import template

from hq.platform.application.page_parts import html_attributes
from hq.platform.core.part_examples import layout
from hq.platform.core.template_parts import Option, Part, slotted

register = template.Library()

# Each way a run of sections lays out, as layout.css draws it, and what it is for.
KINDS = {
    "flow": "as many to a row as keep each at least 22rem wide: the sections that close a page, a run of cards",
    "charts": "the flow at the width a chart keeps its scale, 30rem: a domain's charts",
    "pair": "two columns at most, like beside like: a table beside a table, a chart beside a chart",
    "split": "a main and its companion, sized to what it holds, sharing a row while both keep a readable width",
}


def layout_frame(_name: str, given: dict) -> dict:
    """The class and attributes the partial draws from what a page gave."""

    css = {"flow": "section-flow", "charts": "domain-charts", "pair": "two-col", "split": "split"}
    named = {"id": given["id"]} | {
        option.replace("_", "-"): value for option, value in given.items() if option.startswith("data_")
    }
    return given | {"css": css[given["kind"]], "attributes": html_attributes(named)}


LAYOUT = Part(
    name="layout",
    rules="""
    A run of sibling sections or cards (the sections that close a thing's page, the connection panel's
    identity, link and route, a page's run of cards) is one `{% layout %}`, the flow. It lays as many
    to a row as keep each at least `--flow-basis` wide (22rem), and one to a row where that is the whole
    width. A row's last ones share what it leaves, and each stands on a row as tall as the tallest, a
    section's foot at its frame's bottom. While a member holds an open fold (a chart's data) or a table of
    nine rows or more, its row stops sharing its height and each member stands at its own, so a partner
    is never stretched into an empty box. Every member grows into what its row leaves and none caps its
    width, so every row ends at the flow's end. It is measured against the box it sits in, so the same
    sections pair in a dialog and on a page. A part that needs more room before it shares a row sets a
    wider basis on itself (the connection panel's route); a run of charts is `kind="charts"`, the flow
    at the width a chart keeps its scale. A wide one (a table of four columns or more, a fold, anything
    that is not a section or card) goes across the row.

    The flow paints its members in the order the markup holds them, so what is read aloud and where the
    keyboard goes is what is drawn (WCAG 1.3.2, 2.4.3): nothing reorders them. A page therefore writes
    its wide ones ahead of its short ones, which collect after them to share rows rather than one
    standing beside the gap a wide one leaves. Registry sections say which they are
    (`ServiceSection.spans_row`), and `partials/_page_sections.html` draws one kind per pass,
    `spans_row=True` ahead of a page's short sections and `False` among them. *Gate:* `flows in reading
    order` in the layout survey.

    Content with a narrower companion is `kind="split"`: the two share a row while both keep a readable
    width and wrap to a column when they cannot. A month as the main takes the room, its companion
    keeping its basis beside it; a run of sections in a companion is a flow with `aside=True`, so wrapped
    under the main they pair up. A pair (`kind="pair"`) is two columns at most and pairs like with like:
    a table beside a table, a chart beside a chart, so each row ends together.

    Inside a layout the gap is the separation, so a member's own stacking margin is dead space and a
    member is as narrow as its track. No page writes a layout's class or an auto-fit grid of its own:
    the classes are this part's, and `lint:css` refuses `repeat(auto-fit` and `repeat(auto-fill` outside
    the parts that lay out something else inside themselves. A layout with nothing in it draws nothing.
    """,
    example=(
        '{% layout %}{% section "Example identity" %}<p>An example line.</p>{% endsection %}'
        '{% section "Example link" %}<p>Another example line.</p>{% endsection %}{% endlayout %}'
    ),
    states=layout.LAYOUT_STATES,
    classes=("domain-charts", "section-flow", "split", "two-col"),
    partial="partials/_layout.html",
    about="A run of a page's sections, laid out as its kind says.",
    options={
        "kind": Option(
            default="flow",
            choices=tuple(KINDS),
            about="; ".join(f"`{name}`, {about}" for name, about in KINDS.items()) + ".",
        ),
        "aside": Option(default=False, choices=(False, True), about="Drawn as an `<aside>`, a split's companion."),
        "id": Option(about="The layout's own id, for a region refreshed in place."),
    },
    data=True,
    public=True,
    shape=layout_frame,
)
slotted(LAYOUT, library=register)
