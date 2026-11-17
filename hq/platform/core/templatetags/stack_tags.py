"""The two plain lists a page writes its items into: the stack list, read
down, each item a line of its own, and the dot list, read across, its items
on one line with a dot between each.

``{% stack_list %}<li>{% status "High" tone="serious" %} An example advisory</li>{% endstack_list %}``
draws ``partials/_stack_list.html``: the notes under a row's name, the
statuses a record carries, the records a section lists. ``below="Affected"``
makes it a block the open row shows under that label (``.row-below``).

``{% dot_list %}<li>{% entity link %}</li><li>2 days ago</li>{% enddot_list %}``
draws ``partials/_dot_list.html``: a fact's links, a row's context line, a
band's details. ``muted=True`` says it quietly.
"""

from django import template

from hq.platform.application.page_parts import html_attributes
from hq.platform.core.templatetags.value_tags import Option, Part, slotted

register = template.Library()

STACK_LIST_PARTIAL = "partials/_stack_list.html"
DOT_LIST_PARTIAL = "partials/_dot_list.html"


def data_attributes(given: dict) -> str:
    """The ``data-`` attributes a page gave a list as ``data_*`` options."""

    return html_attributes(
        {option.replace("_", "-"): value for option, value in given.items() if option.startswith("data_")}
    )


def stack_list_frame(_name: str, given: dict) -> dict:
    """What the partial draws from what a page gave: the open row's label for
    a list that waits for it, and its ``data-`` attributes."""

    return {"below": given.get("below") or "", "data": data_attributes(given)}


def dot_list_frame(name: str, given: dict) -> dict:
    """Whether the run is said quietly, ``True`` or ``False`` and never a word
    that would read as one, and its ``data-`` attributes."""

    muted = given["muted"]
    if not isinstance(muted, bool):
        raise template.TemplateSyntaxError(f"{name} muted= is True or False, not {muted!r}")
    return {"muted": muted, "data": data_attributes(given)}


slotted(
    Part(name="stack_list", partial=STACK_LIST_PARTIAL, options={"below": Option()}, data=True, shape=stack_list_frame),
    library=register,
)
slotted(
    Part(
        name="dot_list",
        partial=DOT_LIST_PARTIAL,
        options={"muted": Option(default=False)},
        data=True,
        shape=dot_list_frame,
    ),
    library=register,
)
