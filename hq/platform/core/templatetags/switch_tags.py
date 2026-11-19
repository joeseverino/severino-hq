"""The switch: a row of links to the same view, one of them current, as a
reporting window, a status filter, a tool's tabs or a zone's siblings are."""

from django import template

from hq.platform.core.template_parts import Example, Option, Part, slotted

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
        rules="""
        `{% switch label=... %}{% choice url=... current=... %}...{% endchoice %}{% summary %}...{%
        endswitch %}`: a row of links to the same view (a window, a status, a tab, a sibling), each a
        `scope-chip`, the current one `aria-current="page"`, a choice's `count` beside its chip and its
        `note` and `mark` inside it. A choice of the view's state is a `.scope-chip`, never a chip of
        places to go.
        """,
        example='{% switch label="Example domains" %}{% choice url="/example/one/" current=True %}example.com{% endchoice %}{% choice url="/example/two/" %}example.net{% endchoice %}{% endswitch %}',
        states={
            # A mark and a note on some choices, and the current one's summary under the row.
            "marked": Example(
                '{% switch label="Example domains" %}'
                '{% choice url="/example/one/" current=True mark="★" %}example.com{% endchoice %}'
                '{% choice url="/example/two/" %}example.net{% endchoice %}'
                '{% choice url="/example/three/" note="not managed" %}example-long-domain-name.example.org{% endchoice %}'
                "{% summary %}An example line that says what the current choice answers.{% endswitch %}"
            ),
            # A count beside each chip, nought included.
            "counted": Example(
                '{% switch label="Example status" %}'
                '{% choice url="?" current=True count=128 %}All{% endchoice %}'
                '{% choice url="?status=example-new" count=12 %}Example new{% endchoice %}'
                '{% choice url="?status=example-read" count=0 %}Example read{% endchoice %}'
                "{% endswitch %}"
            ),
        },
        classes=("scope-chip", "switch"),
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
        example='{% switch label="Example views" %}{% choice url="/example/" current=True count=3 %}Example{% endchoice %}{% choice url="/example/all/" note="all" %}Every example{% endchoice %}{% endswitch %}',
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
