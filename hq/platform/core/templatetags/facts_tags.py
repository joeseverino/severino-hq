"""Facts: every label and its value a page shows, drawn by one part, its
``{% facts %}`` around each ``{% fact %}``."""

from django import template
from django.template.base import token_kwargs

from hq.platform.core.template_parts import Option, Part, SlotsNode

register = template.Library()

FACTS_PARTIAL = "partials/_facts.html"
FACT_PARTIAL = "partials/_fact.html"

# Each layout, drawn as its class (``facts-band``) by the partial.
LAYOUTS = ("list", "band", "inline")


class FactTakes(Part):
    """What ``{% fact %}`` takes: its label first, then its options. Given
    ``value``, the option ``bare_with`` names, it encloses nothing."""

    def values(self, given: dict) -> dict:
        given = super().values(given)
        detail = given.get("detail")
        if isinstance(detail, list | tuple):
            return {**given, "detail": "", "lines": tuple(line for line in detail if line)}
        return given


FACTS = Part(
    name="facts",
    classes=("facts-band", "facts-inline", "facts-list"),
    partial=FACTS_PARTIAL,
    about="Every label and its value a page shows, each a `{% fact %}`.",
    options={
        "layout": Option(
            default="list",
            choices=LAYOUTS,
            about=(
                "A list, each label beside its value; a band, the few facts that sum up a page, each a cell; "
                "a line (`inline`), a card's summary, each fact its value then its words."
            ),
        ),
        "label": Option(about="What the whole is, for a reader, when nothing on the page heads it."),
    },
    public=True,
    example='{% facts layout="band" %}{% fact "Running" value=n url=u %}{% endfacts %}',
)
FACT = FactTakes(
    name="fact",
    classes=("band-icon", "fact", "fact-detail", "fact-label", "fact-value", "tone-*"),
    partial=FACT_PARTIAL,
    about=(
        "One fact of a `{% facts %}`: its label, its value and the detail under it. Given `value=` it encloses "
        "nothing; a value in markup is its block, its detail after `{% detail %}`."
    ),
    parts=("value", "detail"),
    ordered={"label": Option(required=True, about="Its label.")},
    options={
        "value": Option(about="Its value, which makes it one tag with no end."),
        "url": Option(about="The page in HQ its value links to; an outside one is the block form's `{% entity %}`."),
        "detail": Option(about="The words under the value, or a list of them, a line each."),
        "tone": Option(about="A status tone the fact is colored by, for what it means."),
        "icon": Option(about="The icon beside it."),
    },
    bare_with="value",
    public=True,
    example='{% fact "Version" icon="layers" %}<code>{{ tag }}</code>{% detail %}Tag only{% endfact %}',
)


def _options(part: Part, bits: list[str], parser) -> dict:
    given = token_kwargs(list(bits), parser)
    if len(given) != len(bits) or not part.admits(set(given)):
        raise template.TemplateSyntaxError(part.refused())
    return given


@register.tag(name="facts")
def compile_facts(parser, token) -> SlotsNode:
    given = _options(FACTS, token.split_contents()[1:], parser)
    content = parser.parse(("endfacts",))
    parser.delete_first_token()
    return SlotsNode(FACTS, {"content": content}, given)


@register.tag(name="fact")
def compile_fact(parser, token) -> SlotsNode:
    bits = token.split_contents()[1:]
    if not bits or "=" in bits[0]:
        raise template.TemplateSyntaxError('fact takes its label first: {% fact "Label" value=... %}')
    given = {"label": parser.compile_filter(bits[0]), **_options(FACT, bits[1:], parser)}
    if FACT.bare_with in given:
        return SlotsNode(FACT, {}, given)
    value = parser.parse(("detail", "endfact"))
    detail = template.NodeList()
    if parser.next_token().contents == "detail":
        detail = parser.parse(("endfact",))
        parser.delete_first_token()
    return SlotsNode(FACT, {"value": value, "detail": detail}, given)


# The template contract reads a tag's parameters from what it wraps.
compile_facts.__wrapped__ = FACTS  # type: ignore[attr-defined]  # a function's attribute, which typeshed does not declare
compile_fact.__wrapped__ = FACT  # type: ignore[attr-defined]  # as above
