"""Links that remember where they were followed from."""

from django import template

from hq.platform.application.url_query import with_query

register = template.Library()


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
