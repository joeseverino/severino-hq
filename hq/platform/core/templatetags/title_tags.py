"""Title meta: the line of facts a page states on its title's line.

``{% title_meta %}{% status "Online" tone="good" %}{% dot %}<code>example-host</code>{% endtitle_meta %}``
draws ``partials/_title_meta.html``, the template a view names as its
``page_aside`` writing only what it says. ``element="ul"`` draws its parts as a
dot list, each an ``<li>`` that wraps whole.
"""

from django import template

from hq.platform.application.page_parts import html_attributes
from hq.platform.core.templatetags.value_tags import slotted

register = template.Library()

TITLE_META_PARTIAL = "partials/_title_meta.html"
ELEMENTS = frozenset(("p", "ul"))


def title_meta_frame(name: str, given: dict) -> dict:
    """What the partial draws from what a page gave: the element its parts
    run in, and its ``data-`` attributes."""

    element = given.get("element") or "p"
    if element not in ELEMENTS:
        raise template.TemplateSyntaxError(f"{name} element= is one of {', '.join(sorted(ELEMENTS))}")
    return {
        "listed": element == "ul",
        "data": html_attributes(
            {option.replace("_", "-"): value for option, value in given.items() if option.startswith("data_")}
        ),
    }


slotted(
    "title_meta",
    TITLE_META_PARTIAL,
    ("content",),
    frozenset(("element",)),
    shape=title_meta_frame,
    library=register,
)
