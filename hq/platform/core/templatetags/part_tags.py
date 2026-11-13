"""The page parts a template writes as tags, each drawn by the partial its
``page_parts`` dataclass draws through, so a page built in a template and one
built in code look the same.

``{% section "Agents" count=n lede="..." %}body{% aside %}a line, a link, a
control{% empty %}What is missing.{% foot %}How to read the body.{% endsection %}``:
a section in its frame (``page_parts.SectionPart``). Its aside, empty and
foot parts may be left out; with no body and no empty part it draws nothing.
``remedy_label`` and ``remedy_url`` are the link the empty line offers,
``more_label`` and ``more_url`` the aside's link to the rest, and ``moment``
(in the ``when`` form) the moment its heading names, which stands for the
heading when the tag is given none.

Not ``Library.simple_block_tag``: Django's parses only up to its end tag, so
it cannot take the later parts, and it renders the body before the tag's
function runs, so a section inside a section could not head itself a level
down. The arguments are still read by Django's own ``parse_bits``.
"""

import inspect

from django import template
from django.template.library import TagHelperNode, parse_bits
from django.utils.safestring import mark_safe

from hq.platform.application.page_parts import LAST_LEVEL, SECTION_PARTIAL, TOP_LEVEL, SectionPart

register = template.Library()

# The render context's own keys for the heading level the next section takes
# and for the heading ids sections have drawn, kept for the whole drawing of
# a page, its includes too.
_LEVEL = "hq.part_tags.level"
_IDS = "hq.part_tags.ids"


class SectionTakes:
    """What ``{% section %}`` takes: its heading, its options and ``data_*``
    attributes, and the parts after its body, which the template contract
    records as its parameters and parts."""

    parts = ("content", "aside", "empty", "foot")
    options = (
        "count",
        "lede",
        "id",
        "fold",
        "open",
        "css",
        "remedy_label",
        "remedy_url",
        "more_label",
        "more_url",
        "figure",
        "moment",
        "when",
        "head",
        "edge",
    )

    def __init__(self) -> None:
        keyword, ordered = inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD
        self.__signature__ = inspect.Signature(
            [
                inspect.Parameter("content", ordered),
                inspect.Parameter("heading", ordered, default=""),
                *(inspect.Parameter(option, keyword, default="") for option in self.options),
                inspect.Parameter("data", inspect.Parameter.VAR_KEYWORD),
            ]
        )

    def __call__(self, *args, **kwargs) -> None:
        raise TypeError(f"section is a template tag, drawn by {SECTION_PARTIAL}")


SECTION = SectionTakes()


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
        return context.template.engine.get_template(SECTION_PARTIAL).render(context.new(values)).strip()


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
        ["heading"],
        None,
        "data",
        ("",),
        options,
        dict.fromkeys(options, ""),
        "section",
    )
    if not args and "moment" not in kwargs:
        raise template.TemplateSyntaxError("section takes its heading, or the moment= its heading names")
    if unknown := sorted(name for name in kwargs if name not in options and not name.startswith("data_")):
        given = ", ".join(unknown)
        raise template.TemplateSyntaxError(f"section takes {', '.join(options)} and data_* options, not {given}")
    found = {name: template.NodeList() for name in SECTION.parts}
    ahead = list(SECTION.parts)
    while True:
        found[ahead.pop(0)] = parser.parse((*ahead, "endsection"))
        marker = parser.next_token().contents
        if marker == "endsection":
            return SectionNode(args, kwargs, found)
        ahead = ahead[ahead.index(marker) :]


# The template contract reads a tag's parameters from what it wraps.
compile_section.__wrapped__ = SECTION  # type: ignore[attr-defined]  # a function's attribute, which typeshed does not declare


class HeadedNode(template.Node):
    def __init__(self, nodelist: template.NodeList) -> None:
        self.nodelist = nodelist

    def render(self, context) -> str:
        root = context.render_context.dicts[0]
        level = root.get(_LEVEL, TOP_LEVEL)
        root[_LEVEL] = min(level + 1, LAST_LEVEL)
        try:
            return self.nodelist.render(context)
        finally:
            root[_LEVEL] = level


@register.tag(name="headed")
def compile_headed(parser, token) -> HeadedNode:
    """``{% headed %}body{% endheaded %}``: a body under a heading its part
    draws itself at the level a section here would take (a dialog's title,
    an insight's), so a section in the body heads itself a level below it."""

    if len(token.split_contents()) != 1:
        raise template.TemplateSyntaxError("headed takes no arguments")
    nodelist = parser.parse(("endheaded",))
    parser.delete_first_token()
    return HeadedNode(nodelist)
