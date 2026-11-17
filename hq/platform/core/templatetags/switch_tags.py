"""The switch: a row of links to the same view, one of them current, as a
reporting window, a status filter, a tool's tabs or a zone's siblings are."""

from django import template

from hq.platform.core.template_parts import Option, Part, slotted

register = template.Library()

SWITCH_PARTIAL = "partials/_switch.html"
CHOICE_PARTIAL = "partials/_switch.html#choice"


def switch_frame(_name: str, given: dict) -> dict:
    """The navigation's name, which tells one switch on a page from another."""

    return {"label": given["label"]}


def choice_frame(_name: str, given: dict) -> dict:
    """One choice: where it leads, whether it is the current one, and its
    count, which is drawn for any number given, nought included."""

    count = given["count"]
    return {
        "url": given["url"],
        "current": bool(given["current"]),
        "count": count,
        "counted": count is not None and count != "",
        "note": given["note"] or "",
        "mark": given["mark"] or "",
    }


slotted(
    Part(
        name="switch",
        partial=SWITCH_PARTIAL,
        about=(
            "A row of links to the same view, each a `{% choice %}`, the current one marked, and under it the "
            "`{% summary %}` part, when given, saying what the current one is."
        ),
        parts=("content", "summary"),
        options={"label": Option(required=True, about="The name of what it switches between.")},
        shape=switch_frame,
    ),
    library=register,
)
slotted(
    Part(
        name="choice",
        partial=CHOICE_PARTIAL,
        about="One choice of a `{% switch %}`, a chip leading to its view.",
        options={
            "url": Option(required=True, about="Where it leads."),
            "current": Option(about="The view being drawn, marked `aria-current`."),
            "count": Option(default=None, about="A figure beside its chip, nought included."),
            "note": Option(about="A quiet word inside the chip."),
            "mark": Option(about="A symbol before its words, hidden from a screen reader."),
        },
        shape=choice_frame,
    ),
    library=register,
)
