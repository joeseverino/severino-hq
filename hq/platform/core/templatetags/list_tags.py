"""The list: every list a page writes its items into, each `<li>`, read the
way its kind says, from one part."""

from dataclasses import dataclass

from django import template
from django.template import Template

from hq.platform.application.page_parts import html_attributes
from hq.platform.core.template_parts import Example, Option, Part, slotted
from hq.platform.core.templatetags.frame_tags import MUTED
lazy from hq.platform.application.pages import Page

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


def _title_line() -> dict:
    """A page head whose title's line states its facts as a muted dots list."""

    return {
        "page": Page(title="example-host-01"),
        "page_aside": Template(
            '{% list kind="dots" muted=True data_example="example" %}'
            '<li>{% status "Online" tone="good" %} since 3 days ago</li>'
            "<li>example role</li><li><code>192.0.2.10</code></li>"
            "<li>also known as <code>example-host-01.example.com</code></li>"
            '<li><strong>{% status "Out of date" tone="attention" pill=False %}</strong> since 2 hours ago</li>'
            '<li>{% status "An example sentence that says what the state means." tone="attention" pill=False %}</li>'
            "{% endlist %}"
        ),
    }


LIST = Part(
    name="list",
    rules="""
    Each item is an `<li>` and nothing stands between them. A list with no class, or a kind's class
    written by hand, is refused by `hand-drawn-list`. A list with no item draws nothing, so a page
    writes each `<li>` under its own test.

    A plain list read down (a row's notes, a record's statuses, a section's records) is `{% list %}`.
    Its `<li>`s each run their parts inline a small gap apart, and an item that reads as a sentence is
    marked `.prose`. In a table's cell its items are the cell's own lines: a status and the line under
    it, links one to a line, a name's notes. They sit at the row's rhythm with no gap, and a line that
    holds a link is a target tall. `below="Label"` makes a `down` list a block the open row shows
    under that label.

    A run of whole parts read across one line (a fact's links, a row's context line, a band's
    details) is `{% list kind="dots" %}`, each part an `<li>`, with `muted=True` for one said quietly.
    The stylesheet draws the dot in the gap before each part, so no template writes a separator and no
    line opens on one. The facts a page states on its title's line (a record's state, a machine's
    addresses) are `{% list kind="dots" muted=True %}`, written by the template its view names as
    `page_aside`, where the stylesheet sets it small.

    A row of places to go (the dashboard's links, a saved copy's own pages) is `{% list kind="chips"
    %}`, each item an `<li>` holding one link, `{% entity url|outside label=... %}` for a page outside
    HQ. Each link is a chip, a target by its frame and never underlined at rest, and an outside one's
    arrow sits quieter than its name. A run of names inside a line stays a `dots` list.
    """,
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
    states={
        # Read down: a status with its words, one whose link stands alone, and a note.
        "down": Example(
            '{% section "Example advisories" %}{% list data_example="example" %}'
            '<li class="prose">{% status "High" tone="serious" %} <a href="/example/advisory/">EXAMPLE-2026-0001</a> '
            "An example advisory long enough to wrap onto a second line on a phone.</li>"
            '<li>{% status "Low" tone="attention" %} <a href="/example/advisory/2/">EXAMPLE-2026-0002</a></li>'
            '<li class="muted">and 2 more on its page</li>'
            "{% endlist %}{% endsection %}"
        ),
        # In a table: a row's notes under its name, and a list the open row shows under its label.
        "in-a-row": Example(
            '{% section "Example checks" %}{% table_region "Example checks" %}<table class="data-table"><thead><tr>'
            '{% table_heading "Check" %}{% table_heading "Result" %}</tr></thead><tbody>'
            "<tr><td>Example reading"
            '{% list %}<li class="muted">An example reason it is not read.</li>'
            '<li class="muted">Another example reason.</li>{% endlist %}</td>'
            '<td class="cell-data">{% status "Failed" tone="serious" %}<br><strong>2 example names differ</strong>'
            '{% list below="Example affected" %}<li><strong>example-proxy</strong>{% dot %}example.com</li>'
            "<li><strong>example-mail</strong>{% dot %}mail.example.com</li>{% endlist %}</td></tr>"
            "</tbody></table>{% endtable_region %}{% endsection %}"
        ),
        # Places to go: one of HQ's own pages, outside ones with their arrow, and a name long
        # enough to wrap on a phone.
        "chips": Example(
            '{% section "Example links" %}{% list kind="chips" %}'
            '<li><a href="/example/" title="An example page of HQ">Example page</a></li>'
            '<li>{% entity "https://example.com/"|outside label="Example site" %}</li>'
            '<li>{% entity "https://docs.example.com/"|outside label="Example documentation" %}</li>'
            '<li>{% entity "https://example.org/"|outside '
            'label="An example tool whose name runs long enough to wrap" %}</li>{% endlist %}{% endsection %}'
        ),
        # The facts on a title's line: long enough to wrap under the title on a phone, each
        # part wrapping whole, a state as a pill and as words among them.
        "title-line": Example(
            '{% include "partials/_page_head.html" %}{% section "Example section 3" %}<p>An example line.</p>'
            "{% endsection %}",
            _title_line,
            heads_the_page=True,
        ),
    },
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
