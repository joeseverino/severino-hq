"""The switch: a row of links to the same view, one of them current, as a
reporting window, a status filter, a tool's tabs or a zone's siblings are.

``{% switch label="Domains" %}{% choice url=u current=True count=3 note="not managed" mark="★" %}example.com{% endchoice %}{% summary %}What the current one is.{% endswitch %}``
draws ``partials/_switch.html``: a labeled navigation list of chips, the
current one marked with ``aria-current``, each choice's count beside its chip
and inside its link, and the summary, when given, under the row.
"""

from django import template

from hq.platform.core.templatetags.value_tags import slotted

register = template.Library()

SWITCH_PARTIAL = "partials/_switch.html"
CHOICE_PARTIAL = "partials/_switch.html#choice"


def _refuse_data(name: str, given: dict) -> None:
    # A shape admits data_* options; nothing reads one on a switch, so one
    # given is a mistake said at once rather than dropped.
    if extra := sorted(option for option in given if option.startswith("data_")):
        raise template.TemplateSyntaxError(f"{name} takes no {', '.join(extra)}")


def switch_frame(name: str, given: dict) -> dict:
    """The navigation's name, which tells one switch on a page from another."""

    _refuse_data(name, given)
    if not given.get("label"):
        raise template.TemplateSyntaxError(f"{name} is given label=, the name of what it switches between")
    return {"label": given["label"]}


def choice_frame(name: str, given: dict) -> dict:
    """One choice: where it leads, whether it is the current one, and its
    count, which is drawn for any number given, nought included."""

    _refuse_data(name, given)
    if not given.get("url"):
        raise template.TemplateSyntaxError(f"{name} is given url=, where the choice leads")
    count = given.get("count")
    return {
        "url": given["url"],
        "current": bool(given.get("current")),
        "count": count,
        "counted": count is not None and count != "",
        "note": given.get("note") or "",
        "mark": given.get("mark") or "",
    }


slotted("switch", SWITCH_PARTIAL, ("content", "summary"), frozenset(("label",)), shape=switch_frame, library=register)
slotted(
    "choice",
    CHOICE_PARTIAL,
    ("content",),
    frozenset(("url", "current", "count", "note", "mark")),
    shape=choice_frame,
    library=register,
)
