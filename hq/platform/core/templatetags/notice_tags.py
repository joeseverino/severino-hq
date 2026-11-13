"""The notice: every banner and message a page shows, in one of its tones.

``{% notice tone="warning" title="2 jobs failed" %}<p>What to do.</p>{% endnotice %}``
draws ``partials/_notice.html``. A message among the page's messages is the
same part in a toast's placement (``.messages``), never a second part.
"""

from django import template

from hq.platform.core.templatetags.value_tags import slotted

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


def notice_frame(name: str, given: dict) -> dict:
    """What the partial draws from what a page gave: the classes of its tone,
    the live region it is (an error interrupts, anything else waits its turn)
    and its ``data-`` attributes, True drawn bare."""

    tone = given.get("tone") or ""
    if tone not in TONES:
        raise template.TemplateSyntaxError(f"{name} tone= is one of {', '.join(sorted(filter(None, TONES)))}")
    return {
        "classes": " ".join((TONES[tone], given.get("css") or "")).strip(),
        "role": "alert" if tone == "error" else "status",
        "title": given.get("title") or "",
        "data": [
            (option.replace("_", "-"), value)
            for option, value in given.items()
            if option.startswith("data_") and value is not None and value is not False and value != ""
        ],
    }


slotted(
    "notice", NOTICE_PARTIAL, ("content",), frozenset(("tone", "title", "css")), shape=notice_frame, library=register
)
