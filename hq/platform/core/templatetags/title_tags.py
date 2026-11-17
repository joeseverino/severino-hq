"""Title meta: the line of facts a page states on its title's line.

``{% title_meta %}<li>{% status "Online" tone="good" %}</li><li><code>example-host</code></li>{% endtitle_meta %}``
draws ``partials/_title_meta.html``, the template a view names as its
``page_aside`` writing only what it says: a dot list, each part an ``<li>``
that wraps whole.
"""

from django import template

from hq.platform.application.page_parts import html_attributes
from hq.platform.core.template_parts import Part, slotted
from hq.platform.core.templatetags.stack_tags import DOT_LIST

register = template.Library()

TITLE_META_PARTIAL = "partials/_title_meta.html"


def title_meta_frame(name: str, given: dict) -> dict:
    """What the partial draws from what a page gave: its ``data-``
    attributes."""

    return {
        "data": html_attributes(
            {option.replace("_", "-"): value for option, value in given.items() if option.startswith("data_")}
        ),
    }


@register.filter
def title_parts(content: str) -> str:
    """The parts as the page wrote them, each an ``<li>``: words between the
    parts would join the line with no dot, or open a line on one."""

    if not (content.startswith("<li") and content.endswith("</li>")):
        raise template.TemplateSyntaxError("title_meta's parts are <li> elements")
    return content


slotted(
    Part(
        name="title_meta",
        classes=("page-title-meta",),
        draws=(DOT_LIST,),
        partial=TITLE_META_PARTIAL,
        about="The line of facts on a page's title line, each an `<li>` that wraps whole, a dot between each.",
        data=True,
        shape=title_meta_frame,
    ),
    library=register,
)
