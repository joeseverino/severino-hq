"""The notice: every banner and message a page shows, in one of its tones."""

from django import template

from hq.platform.application.page_parts import html_attributes
from hq.platform.core.template_parts import Example, Option, Part, slotted

register = template.Library()

NOTICE_PARTIAL = "partials/_notice.html"

# Each tone and its classes, written whole so the stylesheet's tone
# assignment and a search find them. The names are Django's message levels,
# so a message's tags are a tone as they stand.
TONES = {
    "": "notice",
    "info": "notice notice-info",
    "success": "notice notice-success",
    "warning": "notice notice-warning",
    "error": "notice notice-error",
}


def notice_frame(_name: str, given: dict) -> dict:
    """What the partial draws from what a page gave: the classes of its tone,
    the live region it is (an error interrupts, anything else waits its turn)
    and its ``data-`` attributes."""

    tone = given["tone"]
    return {
        "classes": " ".join((TONES[tone], given["css"] or "")).strip(),
        "role": "alert" if tone == "error" else "status",
        "title": given["title"] or "",
        "data": html_attributes(
            {option.replace("_", "-"): value for option, value in given.items() if option.startswith("data_")}
        ),
    }


slotted(
    Part(
        name="notice",
        rules="""
        `{% notice tone="warning" title="2 jobs failed" %}<p>What to do.</p>{% endnotice %}` is every
        banner and message a page shows: a callout in its tone, for what needs saying before the content
        it is about. `tone` is one of Django's message levels, so a message's `level_tag` is its tone as
        it stands; with none it is neutral. An error is an `alert`, which a screen reader says at once,
        and any other a `status`. `title` is its first line, in bold; a title the page composes (words
        around a value, a loop) is the `{% title %}` part, written after the body (`{% notice
        tone="warning" %}<p>Pick another.</p>{% title %}{{ name }} cannot be used{% endnotice %}`). `css`
        and `data_*` are the page's, as a section's are. Among the page's messages (`<ul
        class="messages">`, base.html) the same part is a toast, set closer; it is never a second part.
        Drawn with no body it is the frame a script clones and fills: base.html holds one per tone in a
        `<template data-notice>`. So a line a script adds (`fragment.js`, `demo.js`) is the part the
        server draws, and no script writes a notice's class. An error page is a page of its own, never
        this part.
        """,
        partial=NOTICE_PARTIAL,
        about=(
            "Every banner and message a page shows, in one of its tones; a message among the page's messages is "
            "the same part in a toast's placement. A title the page composes is its `{% title %}` part, after "
            "the body."
        ),
        parts=("content", "title"),
        options={
            "tone": Option(choices=tuple(TONES), about="Its tone, none for a neutral notice; an error is an alert."),
            "title": Option(about="Its first line, in bold."),
            "css": Option(about="The classes of where it stands."),
        },
        data=True,
        public=True,
        example='{% notice tone="warning" title="2 jobs failed" %}<p>What to do.</p>{% endnotice %}',
        states={
            "neutral": Example(
                '{% notice css="example" data_example="example" %}An example line that needs saying first, '
                'with <a href="/example/">an example link</a>.{% endnotice %}'
            ),
            "titled": Example(
                '{% notice tone="warning" title="2 example jobs failed on their last run" %}'
                "<p>example-backup, example-sync</p>{% endnotice %}"
            ),
            # A title the page composes, written as the tag's title part.
            "composed": Example(
                '{% notice tone="warning" %}<p>Pick another below.</p>'
                "{% title %}{{ name|breakable }} cannot be used here{% endnotice %}",
                {"name": "example-target.example.com"},
            ),
            # Holding a switch, as a trace's says what it shows: a link in its words is
            # underlined, a choice's chip is not.
            "switch": Example(
                '{% notice tone="info" %}<p>An example line with <a href="/example/">a link</a>.</p>'
                '{% switch label="Example direction" %}{% choice url="/example/in/" %}Example in{% endchoice %}'
                '{% choice url="/example/both/" current=True %}Example both{% endchoice %}{% endswitch %}'
                "{% title %}Showing example-host-01{% endnotice %}"
            ),
            "error": Example(
                '{% notice tone="error" %}An example failure long enough to take more than one line on a phone, '
                "so its wrapping is read too.{% endnotice %}"
            ),
        },
        shape=notice_frame,
    ),
    library=register,
)
