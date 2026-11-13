"""The stack list: a plain list read down, each item a line of its own.

``{% stack_list %}<li>{% status "High" tone="serious" %} An example advisory</li>{% endstack_list %}``
draws ``partials/_stack_list.html``: the notes under a row's name, the
statuses a record carries, the records a section lists. ``below="Affected"``
makes it a block the open row shows under that label (``.row-below``).
"""

from django import template

from hq.platform.application.page_parts import html_attributes
from hq.platform.core.templatetags.value_tags import slotted

register = template.Library()

STACK_LIST_PARTIAL = "partials/_stack_list.html"


def stack_list_frame(_name: str, given: dict) -> dict:
    """What the partial draws from what a page gave: the open row's label for
    a list that waits for it, and its ``data-`` attributes."""

    return {
        "below": given.get("below") or "",
        "data": html_attributes(
            {option.replace("_", "-"): value for option, value in given.items() if option.startswith("data_")}
        ),
    }


slotted(
    "stack_list",
    STACK_LIST_PARTIAL,
    ("content",),
    frozenset(("below",)),
    shape=stack_list_frame,
    library=register,
)
