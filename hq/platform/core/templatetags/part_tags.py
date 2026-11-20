"""The page parts a template writes as tags, each drawn by its ``page_parts``
dataclass's partial, so a page built in a template and one built in code
look the same.

Not ``Library.simple_block_tag``, which parses only up to its end tag and
renders the body before the function runs, so a nested section could not head
itself a level down.
"""

from dataclasses import Field, fields

from django import template
from django.template.library import TagHelperNode, parse_bits
from django.utils.safestring import mark_safe

from hq.platform.application.page_parts import LAST_LEVEL, SECTION_PARTIAL, TOP_LEVEL, SectionPart
from hq.platform.core.part_examples import section
from hq.platform.core.template_parts import Option, Part, choices_of

register = template.Library()

# The render context's own keys for the heading level the next section takes
# and for the heading ids sections have drawn, kept for the whole drawing of
# a page, its includes too.
_LEVEL = "hq.part_tags.level"
_IDS = "hq.part_tags.ids"


# The fields of SectionPart the tag fills itself rather than take as options:
# its heading first, its parts, its data_* options and its level, and the body
# code draws in place of the tag's own.
_FILLED = frozenset({"heading", "data", "level", "template", "context"})


def _option(field: Field) -> Option:
    return Option(default=field.default, choices=choices_of(field.type), about=field.doc or "")


def _section() -> Part:
    named = {field.name: field for field in fields(SectionPart)}
    parts = ("content", "aside", "empty", "foot")
    return Part(
        name="section",
        rules="""
        `{% section "Agents" count=n lede="..." %}body{% aside %}a line, a link, a control{% empty %}No
        agent has connected yet.{% foot %}How to read the body.{% endsection %}` is a titled part of a
        page: a `<section>` labeled by its heading. Its head stands on the page: the heading with its count
        standing in it, the lede under it and the aside beside it. Its body follows in the frame. After
        the body comes its foot, drawn only with one: the line that says how to read it (what a figure
        means, where the numbers come from), quieter than the body. The heading's line is one control
        tall, so a control beside it adds no height and heads in one row are one height. The aside is one
        box at the row's end, so its parts (a count, a dot, a link) wrap as one line of text, and it keeps
        to one line while the row has room for it. What draws its own edges, cells or fill fills the frame
        with no inset and no second border, as a card's flush list group does in Bootstrap. That is a
        table's region, a band of facts, a row of metrics, a month's grid, or a list whose rows carry
        their own padding. Prose, facts and the empty line sit in the frame's padding. With no body it
        draws its empty part as the empty state's line, followed by the link that fills it when
        `remedy_label` and `remedy_url` name one (`{% section "Receipts" remedy_label="Upload one"
        remedy_url=url %}{% empty %}No receipts.{% endsection %}`), and with neither it draws nothing.

        `more_label` and `more_url` end the aside with the quiet link to the whole of what the section
        shows part of ("View Estate", "All runs"), after a dot when the aside says something too, never a
        foot under the list. With no address the link is left out, so a page passes the one it has. A
        line that dates or sources a section goes beside its heading, not loose under it. A section whose
        rows are the latest of more names the rest as its `more_label` and `more_url`.

        `figure=True` draws the section as a `<figure>`, its head the `<figcaption>`, for a drawing (a
        chart, a calendar) whose title, line under it and legend are a section's head like any other. A
        section inside a section heads itself a level down inside the outer frame, and so does one inside
        any other frame (a card, a form, a band's cell). The frame's `--surface-inset` is 0 there, so its
        head draws no rule and its body adds no frame or inset of its own. A part that draws its own title
        (a dialog, an insight card) wraps what it holds in `{% headed %}…{% endheaded %}`, so a section
        inside heads itself a level under that title. A `<pre>` in a section's body wraps and scrolls only
        in height. Two of one heading name their own ids, and `id` is the section's own (a link to it, a
        region refreshed in place).

        `fold=True` draws it closed with its head as its toggle (`open`, any truthy value such as a form's
        errors, starts it open), its count beside it; never a separate "Show all". What it holds sits on
        a panel like its neighbors: a fold of one table has the table's region, and any other fold is a
        folded section, its body in the section's frame. `css` and `data_*` are the section's, on a
        landmark that never pads itself, whatever `css` names: the frame holds the padding.

        A section draws its head inside its frame, over a rule, unless its body is one table region and
        nothing else. Then its head stands above the frame so the table reaches its edges (`:has()` in
        surfaces.css decides; a page picks nothing). `head="above"` or `head="inside"` overrides it for a
        page that needs the other look. Frames that share a row start together, so in a pair (`{% layout kind="pair" %}`)
        a table section beside a section whose body is anything else takes its head inside its frame;
        alone, or beside another table, it keeps its head above. A section's heading that follows a form,
        a table or a list in the same box starts a section of its own: `.section-head` clears what is
        above it by the stack gap, so it is never closer to what came before than to what it heads.

        `edge` takes a status tone (`{% section "Request" edge=entry.tone %}`) for a section that stands
        out from those beside it while it is in a state, such as a request waiting for a decision. The
        tone runs down the starting edge of whichever box frames it, and with no tone it has no edge.

        A heading is plain words, never markup a filter draws (`|when`): `SectionPart` refuses markup in
        it, and a template meets that only as it renders. A heading that names a moment gives it as
        `moment`, in the `when` form `|when` takes, and the part draws the `<time>`: after the heading's
        words, quieter than them (`{% section day.name moment=day.day when="day" %}`), or as the whole
        heading when it has none (`{% section moment=month when="month" %}`). A page built in code builds
        the same part as `hq_sdk.ui.SectionPart`, its body drawn from `template` and `context`, and writes
        `{{ part }}`. The part is the head, the frame and the spacing between, so a page states the
        content and never the spacing, the frame or the count's look.
        """,
        classes=("section-aside", "section-body", "section-head", "section-lede"),
        pieces=("partials/_section_head.html",),
        partial=SECTION_PARTIAL,
        about="A titled part of a page in its frame, its head on the page and its body in the frame.",
        parts=parts,
        ordered={"heading": _option(named["heading"])},
        options={name: _option(field) for name, field in named.items() if name not in _FILLED | set(parts)},
        data=True,
        public=True,
        example='{% section "Agents" count=n %}<p>Body</p>{% aside %}A line.{% empty %}None yet.{% endsection %}',
        states=section.SECTION_STATES,
    )


SECTION = _section()


class SectionNode(TagHelperNode):
    def __init__(self, args, kwargs, parts: dict[str, template.NodeList]) -> None:
        super().__init__(SECTION, takes_context=False, args=args, kwargs=kwargs)
        self.child_nodelists = tuple(f"nodelist_{name}" for name in parts)
        for name, nodelist in parts.items():
            setattr(self, f"nodelist_{name}", nodelist)

    def _rendered(self, name: str, context) -> str:
        # Rendered by the page's own template, so escaped already.
        return mark_safe(getattr(self, f"nodelist_{name}").render(context).strip())  # noqa: S308 - the template's own escaped output

    def render(self, context) -> str:
        args, options = self.get_resolved_arguments(context)
        SECTION.held(options)
        data = {name.removeprefix("data_"): options.pop(name) for name in list(options) if name.startswith("data_")}
        heading = args[0] if args else ""
        options["moment"] = options.get("moment") or None
        root = context.render_context.dicts[0]
        level = root.get(_LEVEL, TOP_LEVEL)
        part = SectionPart(heading=heading, level=level, data=data, **options)
        root[_LEVEL] = min(level + 1, LAST_LEVEL)
        try:
            body, aside, empty, foot = (self._rendered(name, context) for name in SECTION.parts)
        finally:
            root[_LEVEL] = level
        values = part.drawn(body=body, aside=aside, empty=empty, foot=foot, heading_id=_unique(root, part.heading_id))
        if values is None:
            return ""
        return context.template.engine.get_template(SECTION.partial).render(context.new(values)).strip()


def _unique(root: dict, heading_id: str) -> str:
    """The heading id this drawing takes: the part's own, numbered past any
    other section on this page drew, so no two share one."""

    drawn = root.setdefault(_IDS, set())
    unique, number = heading_id, 1
    while unique in drawn:
        number += 1
        unique = f"{heading_id}-{number}"
    drawn.add(unique)
    return unique


@register.tag(name="section")
def compile_section(parser, token) -> SectionNode:
    options = list(SECTION.options)
    args, kwargs = parse_bits(
        parser,
        token.split_contents()[1:],
        list(SECTION.ordered),
        None,
        "data",
        tuple(option.default for option in SECTION.ordered.values()),
        options,
        {name: option.default for name, option in SECTION.options.items()},
        "section",
    )
    if not args and "moment" not in kwargs:
        raise template.TemplateSyntaxError("section takes its heading, or the moment= its heading names")
    if not SECTION.admits(set(kwargs)):
        raise template.TemplateSyntaxError(SECTION.refused())
    found = {name: template.NodeList() for name in SECTION.parts}
    ahead = list(SECTION.parts)
    while True:
        found[ahead.pop(0)] = parser.parse((*ahead, "endsection"))
        marker = parser.next_token().contents
        if marker == "endsection":
            return SectionNode(args, kwargs, found)
        ahead = ahead[ahead.index(marker) :]


# The template contract reads a tag's declaration from what it wraps.
compile_section.__wrapped__ = SECTION  # type: ignore[attr-defined]  # a function's attribute, which typeshed does not declare


class HeadedNode(template.Node):
    def __init__(self, nodelist: template.NodeList, *, top: bool = False) -> None:
        self.nodelist = nodelist
        self.top = top

    def render(self, context) -> str:
        root = context.render_context.dicts[0]
        level = root.get(_LEVEL, TOP_LEVEL)
        root[_LEVEL] = TOP_LEVEL + 1 if self.top else min(level + 1, LAST_LEVEL)
        try:
            return self.nodelist.render(context)
        finally:
            root[_LEVEL] = level


@register.tag(name="headed")
def compile_headed(parser, token) -> HeadedNode:
    """``{% headed %}body{% endheaded %}`` (its Part's rules)."""

    bits = token.split_contents()[1:]
    if bits not in ([], ["top"]):
        raise template.TemplateSyntaxError("headed takes nothing, or top")
    nodelist = parser.parse(("endheaded",))
    parser.delete_first_token()
    return HeadedNode(nodelist, top=bool(bits))


# The template contract reads a tag's declaration from what it wraps.
compile_headed.__wrapped__ = Part(  # type: ignore[attr-defined]  # a function's attribute, which typeshed does not declare
    name="headed",
    about="A body under a heading its part draws itself, so a section in it heads itself a level below.",
    rules="""
    A part that draws its own title (a dialog's, an insight's) wraps what it holds in `{% headed %}`, so
    a section in it heads itself a level under the one a section where the part stands would take.
    `{% headed top %}` is a body under a title at the top level wherever the part stands (a dialog's),
    so a dialog opened from inside a section still heads its own sections a level under its title.
    """,
    # A title a part draws itself, as a dialog does, and a section under it.
    example=(
        '<h2>Example dialog</h2>{% headed top %}{% section "Example detail" %}'
        "<p>An example line under it.</p>{% endsection %}{% endheaded %}"
    ),
)
