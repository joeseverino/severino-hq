"""The notice: every banner and message a page shows, in one of its tones."""

from django import template

from hq.platform.application.page_parts import html_attributes
from hq.platform.core.template_parts import Option, Part, slotted

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
        shape=notice_frame,
    ),
    library=register,
)
