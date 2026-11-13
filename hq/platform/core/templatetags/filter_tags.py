"""The filter bar: every query a page asks of what it lists, in one bar.

``{% filter_bar filtered=narrowed clear=every_url shown=3 total=12 %}{% select "kind" kinds every="kind" %}{% endfilter_bar %}``
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
    narrows, its count and what it holds back, and its ``data-`` attributes."""

    filtered = bool(given.get("filtered"))
    if filtered and not given.get("clear"):
        raise template.TemplateSyntaxError(f"{name} filtered=True is given clear=, the page with nothing narrowed")
    shown, total = (given.get(option) for option in ("shown", "total"))
    counted = shown not in (None, "")
    if counted != (total not in (None, "")):
        raise template.TemplateSyntaxError(f"{name} takes shown= and total= together")
    held = tuple(given.get("held") or ())
    if held and not given.get("more"):
        raise template.TemplateSyntaxError(f"{name} held= is given more=, the page that shows what it holds back")
    return {
        "classes": " ".join(("filter-bar", given.get("css") or "")).strip(),
        "action": given.get("action") or "",
        "verb": given.get("verb") or "Filter",
        "clear": given.get("clear") if filtered else "",
        "shown": f"{shown:,}" if counted else "",
        "total": f"{total:,}" if counted else "",
        "held": held,
        "more": given.get("more") if held else "",
        "data": html_attributes(
            {option.replace("_", "-"): value for option, value in given.items() if option.startswith("data_")}
        ),
    }


slotted(
    "filter_bar",
    FILTER_BAR_PARTIAL,
    ("content", "summary"),
    frozenset(("filtered", "clear", "verb", "action", "css", "shown", "total", "held", "more")),
    shape=filter_bar_frame,
    library=register,
)
