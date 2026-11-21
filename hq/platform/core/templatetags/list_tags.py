"""The list: every list a page writes its items into, each `<li>`, read the
way its kind says, from one part, and the run of short parts a table's cell
shows as many of as its line holds."""

from dataclasses import dataclass
from typing import Any

from django import template
from django.template import Template
from django.template.base import token_kwargs
from django.utils.html import format_html
from django.utils.safestring import mark_safe

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
    # The class its list is drawn with.
    css: str
    option: str = ""


KINDS = {
    "down": Kind(
        "read down, each item a line of its own: the notes under a row's name, a record's statuses",
        "stack-list",
        option="below",
    ),
    "dots": Kind(
        "a run on one line, a dot between each that no line opens on: a fact's links, a row's context line",
        "dot-list",
        option="muted",
    ),
    "prose": Kind("sentences, each marked as the browser marks it", "prose-list", option="numbered"),
    "chips": Kind("places to go, each item one link drawn as a chip of its own", "link-chips"),
    "codes": Kind("names in code, wrapping between them: policy names, ports, permissions", "code-list"),
    "labeled": Kind("each item a label then its value, the labels in one column: addresses", "address-list"),
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
    """A page head whose line under its title states its facts as a muted dots list."""

    return {
        "page": Page(title="example-host-01"),
        "page_aside": Template(
            '{% list kind="dots" muted=True data_example="example" %}'
            '<li>{% status "Online" tone="good" %} since Oct 3</li>'
            "<li>example role</li><li><code>192.0.2.10</code></li>"
            "<li>also known as <code>example-host-01.example.com</code></li>"
            '<li><strong>{% status "Out of date" tone="attention" pill=False %}</strong> since 9:29 AM</li>'
            '<li>{% status "An example sentence that says what the state means." tone="attention" pill=False %}</li>'
            "{% endlist %}"
        ),
    }


LIST = Part(
    name="list",
    rules="""
    Each item is an `<li>` and nothing stands between them. A list with no class, or a kind's class
    written by hand, is refused by the `list` shape. A list with no item draws nothing, so a page
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
    line opens on one. The facts a page states under its title (a record's state, a machine's
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
        # The facts under a title: long enough to wrap under the title on a phone, each
        # part wrapping whole, a state as a pill and as words among them.
        "title-line": Example(
            '{% include "partials/_page_head.html" %}{% section "Example section 3" %}<p>An example line.</p>'
            "{% endsection %}",
            _title_line,
            heads_the_page=True,
        ),
    },
    classes=tuple(kind.css for kind in KINDS.values()),
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


# How many of a line list's parts show on its row, wrapping with its column
# as any value's words do; the whole run shows on the row's open line.
LINE_LEAD = 3


class LineListNode(template.Node):
    def __init__(self, nodelist: Any, values: Any, name: str, kind: Any) -> None:
        self.nodelist = nodelist
        self.values = values
        self.name = name
        self.kind = kind

    def _parts(self, context: Any, values: list[Any]) -> str:
        drawn = []
        for value in values:
            with context.push(**{self.name: value}):
                drawn.append(self.nodelist.render(context))
        return "".join(drawn)

    def render(self, context: Any) -> str:
        values = [value for value in self.values.resolve(context) or () if value not in (None, "")]
        if not values:
            return ""
        kind = self.kind.resolve(context) if self.kind else ""
        LINE_LIST.held({"kind": kind})
        lead, rest = values[:LINE_LEAD], values[LINE_LEAD:]
        # Parts already rendered and escaped by their own template.
        parts = mark_safe(self._parts(context, lead))  # noqa: S308 - rendered and escaped already
        tail = mark_safe(self._parts(context, rest))  # noqa: S308 - rendered and escaped already
        # The rest shows only on the row's open line (table_layout.HELD).
        if kind:
            css = KINDS[kind].css
            more = format_html('<li class="line-rest"><ul class="{} line-run">{}</ul></li>', css, tail) if rest else ""
            return format_html('<ul class="{} line-list">{}{}</ul>', css, parts, more)
        more = format_html('<span class="line-rest"><span class="line-run">{}</span></span>', tail) if rest else ""
        return format_html('<span class="line-list">{}{}</span>', parts, more)


@register.tag("line_list")
def line_list(parser: Any, token: Any) -> LineListNode:
    bits = token.split_contents()
    options = token_kwargs(bits[4:], parser)
    if len(bits) not in (4, 5) or bits[2] != "as" or len(options) != len(bits[4:]) or set(options) - {"kind"}:
        raise template.TemplateSyntaxError('line_list takes values "as" name, then optionally kind=')
    nodelist = parser.parse(("endline_list",))
    parser.delete_first_token()
    return LineListNode(nodelist, parser.compile_filter(bits[1]), bits[3], options.get("kind"))


# The template contract reads a tag's declaration from what it wraps.
LINE_LIST = line_list.__wrapped__ = Part(  # type: ignore[attr-defined]  # a function's attribute, which typeshed does not declare
    name="line_list",
    about="A run of short parts in a table's cell, each drawn by its body, as many on the row as its line holds.",
    rules="""
    A run of short parts in a cell (a project's technologies, what a connection reads or reaches) is
    `{% line_list values as value %}…{% endline_list %}`, with `kind=` one of the list's kinds where
    the body draws each `li`. No count decides how many show. Its first three size the column and
    wrap with it. The rest ask the column for nothing and show as many as the line they end has room
    for, each whole, and the row opens only when one is cut. A list folded at a fixed number behind
    `row-more` is refused (`test_template_line_lists`).
    """,
    ordered={"values": Option(required=True, about="The parts, each drawn by the body as the name after `as`.")},
    options={
        "kind": Option(
            choices=("", *KINDS),
            about="The list kind whose `<li>`s the body draws; left out, a run of parts that is no list.",
        )
    },
    # Four parts that all show, two, and nine. With script the parts past the
    # first three show as far as their line has room and the row opens for the
    # rest; without it, the rest wrap on lines of their own beneath the first three.
    example=Example(
        '{% section "Example stacks" %}{% table_region "Example stacks" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Name" "identifier" %}{% table_heading "Parts" %}</tr></thead><tbody>'
        "{% for row in rows %}<tr><td>{{ row.0 }}</td><td>{% line_list row.1 as part %}{% status part chip=True %}"
        "{% endline_list %}</td></tr>{% endfor %}</tbody></table>{% endtable_region %}{% endsection %}",
        {
            "rows": (
                ("example-01", ("Debian", "systemd", "Caddy", "Tailscale")),
                ("example-02", ("Django", "SQLite")),
                ("example-03", tuple(f"example-part-{at:02}" for at in range(1, 10))),
            )
        },
    ),
    states={
        # A run its body draws as a list's items, a dot between each.
        "dots": Example(
            '{% section "Example reads" %}{% table_region "Example reads" %}<table class="data-table"><thead><tr>'
            '{% table_heading "Connection" "identifier" %}{% table_heading "Reads" %}</tr></thead><tbody>'
            '{% for row in rows %}<tr><td>{{ row.0 }}</td><td>{% line_list row.1 as read kind="dots" %}'
            "<li><code>{{ read }}</code></li>{% endline_list %}</td></tr>{% endfor %}</tbody></table>"
            "{% endtable_region %}{% endsection %}",
            {
                "rows": (
                    ("example-main", ("example.read", "example.list")),
                    ("example-admin", ("example.read", "example.write", "example.admin", "example.audit")),
                )
            },
        )
    },
)
