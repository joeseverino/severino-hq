"""Links that carry a query: what they ask, or where they were followed from."""

from typing import Annotated, Any

from django import template

from hq.platform.application.describe import Describe
from hq.platform.application.url_query import with_query
from hq.platform.core.template_parts import Part, slotted

register = template.Library()


def query_added(
    url: Annotated[str, Describe("The address, which may hold a query of its own.")],
    **query: Annotated[Any, Describe("Each name=value added to it.")],
) -> str:
    """``url`` with each ``name=value`` given added to its own query, encoded."""

    return with_query(url, query)


# A tag rather than a filter: a filter takes one argument, and an address
# asks for one value or several, read from the page, which the tag encodes.
slotted(
    Part(
        name="with_query",
        about=(
            "An address with a query added to its own, each value encoded: written where it stands, or kept "
            "with `as` for the part that links it."
        ),
        function=query_added,
        public=True,
        example='{% with_query "/example/" kind="host" as hosts_url %}{% button "Hosts" hosts_url %}',
    ),
    library=register,
)


@register.filter
def returning_to(url: str, origin: str) -> str:
    """Append the page being left, so the form can come back to it.

    Carried explicitly rather than inferred from Referer: a form reached from a
    service, a domain and a list should return to whichever of those it was,
    and only the link knows. The value is checked again server-side before any
    redirect uses it: a query parameter is not evidence of anything.
    """

    return with_query(url, {"next": str(origin)}) if origin else url


@register.filter
def dictkey(mapping, key):
    """One value out of a mapping, by a key the template holds in a variable.

    Django deliberately has no syntax for this, and the alternative is building
    a parallel list in the view whose order has to stay in step with the
    declaration it mirrors.
    """

    try:
        return mapping.get(key, "")
    except AttributeError:
        return ""
