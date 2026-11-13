"""The filter bar: every query a page asks of what it lists, in one bar.

``{% filter_bar filtered=narrowed clear=every_url %}<select name="kind">...</select>{% summary %}3 of 12{% endfilter_bar %}``
draws ``partials/_filter_bar.html``. A list's table toolbar is the same bar
(``partials/_table_toolbar.html``), never a second one.
"""

from django import template

from hq.platform.application.page_parts import html_attributes
from hq.platform.core.templatetags.value_tags import slotted

register = template.Library()

FILTER_BAR_PARTIAL = "partials/_filter_bar.html"


def filter_bar_frame(name: str, given: dict) -> dict:
    """What the partial draws from what a page gave: its classes, the verb
    that sends it (Filter unless told), the way back to everything when it
    narrows, and its ``data-`` attributes."""

    filtered = bool(given.get("filtered"))
    if filtered and not given.get("clear"):
        raise template.TemplateSyntaxError(f"{name} filtered=True is given clear=, the page with nothing narrowed")
    return {
        "classes": " ".join(("filter-bar", given.get("css") or "")).strip(),
        "action": given.get("action") or "",
        "verb": given.get("verb") or "Filter",
        "clear": given.get("clear") if filtered else "",
        "data": html_attributes(
            {option.replace("_", "-"): value for option, value in given.items() if option.startswith("data_")}
        ),
    }


slotted(
    "filter_bar",
    FILTER_BAR_PARTIAL,
    ("content", "summary"),
    frozenset(("filtered", "clear", "verb", "action", "css")),
    shape=filter_bar_frame,
    library=register,
)
