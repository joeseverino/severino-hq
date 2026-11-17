"""The code patch: a block of code to paste somewhere else, with what to know
about it."""

from django import template

from hq.platform.core.template_parts import Option, Part, slotted

register = template.Library()

PATCH_PARTIAL = "partials/_patch.html"


slotted(
    Part(
        name="patch",
        classes=("patch", "patch-head"),
        partial=PATCH_PARTIAL,
        about=(
            "A block of code to paste somewhere else: its `title` part beside the button that copies it, the "
            "`detail` part's words under it, then its `notes` part's `<li>`s; with no code the notes stand alone."
        ),
        parts=("content", "title", "detail", "notes"),
        options={"id": Option(about="The anchor a link to it names.")},
    ),
    library=register,
)
