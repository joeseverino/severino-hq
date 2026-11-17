"""The two plain lists a page writes its items into: the stack list, read
down, each item a line of its own, and the dot list, read across, its items
on one line with a dot between each."""

from django import template

from hq.platform.application.page_parts import html_attributes
from hq.platform.core.template_parts import Option, Part, slotted

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

    return {"below": given["below"] or "", "data": data_attributes(given)}


def dot_list_frame(_name: str, given: dict) -> dict:
    """Whether the run is said quietly, and its ``data-`` attributes."""

    return {"muted": given["muted"], "data": data_attributes(given)}


slotted(
    Part(
        name="stack_list",
        partial=STACK_LIST_PARTIAL,
        about=(
            "A list read down, each `<li>` a line of its own: the notes under a row's name, the statuses a record "
            "carries, the records a section lists."
        ),
        options={"below": Option(about="The open row's label, for a list the open row shows under it.")},
        data=True,
        shape=stack_list_frame,
    ),
    library=register,
)
slotted(
    Part(
        name="dot_list",
        partial=DOT_LIST_PARTIAL,
        about=(
            "A run of whole parts on one line, each an `<li>`, a dot between each that no line opens on: "
            "a fact's links, a row's context line, a band's details."
        ),
        options={"muted": Option(default=False, choices=(False, True), about="Said quietly.")},
        data=True,
        public=True,
        example="{% dot_list %}<li>{% entity link %}</li><li>2 days ago</li>{% enddot_list %}",
        shape=dot_list_frame,
    ),
    library=register,
)
