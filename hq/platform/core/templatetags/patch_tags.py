"""The code patch: a block of code to paste somewhere else, with what to know
about it.

``{% patch id="hardening" %}services: ...{% title %}Add to <code>compose.yaml</code>{% detail %}Then run it.{% notes %}<li>A note</li>{% endpatch %}``
draws ``partials/_patch.html``: the title beside a Copy button, the code in a
``<pre>`` the button copies, the words under it, and the notes list. With no
code it draws the notes alone.
"""

from django import template

from hq.platform.core.templatetags.value_tags import slotted

register = template.Library()

PATCH_PARTIAL = "partials/_patch.html"


def patch_frame(name: str, given: dict) -> dict:
    """The part's id, which a link to it names, and its code's, which the Copy
    button names (`data-copy-target`)."""

    if extra := sorted(option for option in given if option.startswith("data_")):
        raise template.TemplateSyntaxError(f"{name} takes no {', '.join(extra)}")
    if not given.get("id"):
        raise template.TemplateSyntaxError(f"{name} is given id=, which its Copy button names its code by")
    return {"id": given["id"], "code_id": f"{given['id']}-code"}


slotted(
    "patch",
    PATCH_PARTIAL,
    ("content", "title", "detail", "notes"),
    frozenset(("id",)),
    shape=patch_frame,
    library=register,
)
