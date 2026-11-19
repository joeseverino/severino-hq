"""The list: every list a page writes its items into, each `<li>`, read the
way its kind says, from one part."""

from dataclasses import dataclass

from django import template

from hq.platform.application.page_parts import html_attributes
from hq.platform.core.template_parts import Option, Part, slotted
from hq.platform.core.templatetags.frame_tags import MUTED

register = template.Library()

LIST_PARTIAL = "partials/_list.html"


@dataclass(frozen=True)
class Kind:
    """One way a list reads, and the option only it takes."""

    about: str
    option: str = ""


KINDS = {
    "down": Kind(
        "read down, each item a line of its own: the notes under a row's name, a record's statuses",
        option="below",
    ),
    "dots": Kind(
        "a run on one line, a dot between each that no line opens on: a fact's links, a row's context line",
        option="muted",
    ),
    "prose": Kind("sentences, each marked as the browser marks it", option="numbered"),
    "chips": Kind("places to go, each item one link drawn as a chip of its own"),
    "codes": Kind("names in code, wrapping between them: policy names, ports, permissions"),
    "labeled": Kind("each item a label then its value, the labels in one column: addresses"),
}
# The options a kind takes as its own, each refused beside any other kind.
OWN = {kind.option: name for name, kind in KINDS.items() if kind.option}


def data_attributes(given: dict) -> str:
    """The ``data-`` attributes a page gave a list as ``data_*`` options."""

    return html_attributes(
        {option.replace("_", "-"): value for option, value in given.items() if option.startswith("data_")}
    )


def list_frame(_name: str, given: dict) -> dict:
    """What the partial draws from what a page gave, with its ``data-``
    attributes. An option of another kind is refused, since that kind draws
    nothing of it."""

    for option, owner in OWN.items():
        if given[option] and owner != given["kind"]:
            raise template.TemplateSyntaxError(f'list {option}= is for kind="{owner}"')
    return given | {"data": data_attributes(given)}


@register.filter
def list_items(content: str) -> str:
    """The items as the page wrote them, each an ``<li>``: words between them
    would join a run with no dot, or stand outside every line."""

    if not (content.startswith("<li") and content.endswith("</li>")):
        raise template.TemplateSyntaxError("a list's items are <li> elements")
    return content


LIST = Part(
    name="list",
    example=(
        '{% list %}<li>{% status "High" tone="serious" %}</li><li>An example advisory</li>{% endlist %}'
        '{% list kind="dots" %}<li><a href="/example/">example-host</a></li><li>2 days ago</li>{% endlist %}'
        '{% list kind="prose" %}<li>An example sentence.</li><li>Another example sentence.</li>{% endlist %}'
        '{% list kind="chips" %}<li><a href="/example/">Example page</a></li>'
        '<li><a href="/example/2/">Another example page</a></li>{% endlist %}'
        '{% list kind="codes" %}<li><code>example:read</code></li><li><code>example:write</code></li>{% endlist %}'
        '{% list kind="labeled" %}<li><span>Example network</span> <code>192.0.2.10</code></li>'
        "<li><span>Example tailnet</span> <code>192.0.2.20</code></li>{% endlist %}"
    ),
    classes=("stack-list", "dot-list", "prose-list", "link-chips", "code-list", "address-list"),
    draws=(MUTED,),
    partial=LIST_PARTIAL,
    about="A list, each `<li>` an item, read as its kind says.",
    options={
        "kind": Option(
            default="down",
            choices=tuple(KINDS),
            about="; ".join(f"`{name}`, {kind.about}" for name, kind in KINDS.items()) + ".",
        ),
        "below": Option(about="The open row's label, for a `down` list the open row shows under it."),
        "muted": Option(default=False, choices=(False, True), about="A `dots` run said quietly."),
        "numbered": Option(default=False, choices=(False, True), about="`prose` items read in order, each numbered."),
    },
    data=True,
    public=True,
    shape=list_frame,
)
slotted(LIST, library=register)
