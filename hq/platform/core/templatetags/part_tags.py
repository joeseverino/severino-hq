"""The page parts a template writes as tags, each drawn by the partial its
``page_parts`` dataclass draws through, so a page built in a template and one
built in code look the same, and the tag's options are the dataclass's fields.

Not ``Library.simple_block_tag``: Django's parses only up to its end tag, so
it cannot take the later parts, and it renders the body before the tag's
function runs, so a section inside a section could not head itself a level
down. The arguments are still read by Django's own ``parse_bits``.
"""

from dataclasses import Field, fields

from django import template
from django.template.library import TagHelperNode, parse_bits
from django.utils.safestring import mark_safe

from hq.platform.application.page_parts import LAST_LEVEL, SECTION_PARTIAL, TOP_LEVEL, SectionPart
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
    if unknown := sorted(name for name in kwargs if name not in options and not name.startswith("data_")):
        raise template.TemplateSyntaxError(f"{SECTION.refused()}, not {', '.join(unknown)}")
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
    """``{% headed %}body{% endheaded %}``: a body under a heading its part
    draws itself at the level a section here would take (a dialog's title,
    an insight's), so a section in the body heads itself a level below it.
    ``{% headed top %}`` is a body under a title drawn at the top level
    wherever the part stands (a dialog's), so a dialog opened from inside a
    section still heads its own sections a level under its title."""

    bits = token.split_contents()[1:]
    if bits not in ([], ["top"]):
        raise template.TemplateSyntaxError("headed takes nothing, or top")
    nodelist = parser.parse(("endheaded",))
    parser.delete_first_token()
    return HeadedNode(nodelist, top=bool(bits))
