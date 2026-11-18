"""Facts: every label and its value a page shows, drawn by one part, its
``{% facts %}`` around each ``{% fact %}``."""

from django import template
from django.template.base import token_kwargs

from hq.platform.application.page_parts import html_attributes
from hq.platform.core.template_parts import Option, Part, SlotsNode

register = template.Library()

FACTS_PARTIAL = "partials/_facts.html"
FACT_PARTIAL = "partials/_fact.html"

# Each layout, drawn as its class (``facts-band``) by the partial.
LAYOUTS = ("list", "band", "inline")
# How large a band sets its values: a strip of small figures read across
# (``small``), a page's status facts, its headline figures (``large``), or
# headline figures led by the first (``lead``).
SIZES = ("small", "regular", "large", "lead")


def _attributes(_name: str, given: dict) -> dict:
    """What a page gave, each ``data_*`` option as an attribute of the element."""

    data = {option.replace("_", "-"): value for option, value in given.items() if option.startswith("data_")}
    return {
        **{option: value for option, value in given.items() if not option.startswith("data_")},
        "attributes": html_attributes(data),
    }


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
    classes=("facts-band", "facts-inline", "facts-list", "is-large", "is-lead", "is-small"),
    partial=FACTS_PARTIAL,
    about=(
        "Every label and its value a page shows, each a `{% fact %}`; given `items=` it draws each item "
        "as a fact and encloses nothing."
    ),
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
        "size": Option(
            default="regular",
            choices=SIZES,
            about=(
                "How large a band sets its values: small figures read across, status facts, the headline "
                "figures a page is read for, or headline figures led by the first."
            ),
        ),
        "items": Option(about="Figures (`Kpi`) to draw as its facts, which makes it one tag with no end."),
    },
    data=True,
    bare_with="items",
    shape=_attributes,
    public=True,
    example='{% facts layout="band" %}{% fact "Running" value="3" url="/example/" %}{% endfacts %}',
)
FACT = FactTakes(
    name="fact",
    classes=(
        *("band-icon", "fact", "fact-detail", "fact-figure", "fact-label", "fact-open", "fact-panel"),
        *("fact-panel-foot", "fact-panel-head", "fact-value", "is-zero", "tone-*"),
    ),
    partial=FACT_PARTIAL,
    about=(
        "One fact of a `{% facts %}`: its label, its value and the detail under it. Given `value=` it encloses "
        "nothing; a value in markup is its block, its detail after `{% detail %}`, and a panel its value "
        "opens in place after `{% heading %}`, `{% panel %}` and `{% foot %}`."
    ),
    parts=("value", "detail", "heading", "panel", "foot"),
    ordered={"label": Option(required=True, about="Its label.")},
    options={
        "value": Option(about="Its value, which makes it one tag with no end."),
        "url": Option(about="The page in HQ its value links to; an outside one is the block form's `{% entity %}`."),
        "modal": Option(about="The dialog its value's link opens where script runs."),
        "id": Option(about="The id of the panel its value opens in place, given a `panel` part."),
        "title": Option(about="What pointing at it says."),
        "detail": Option(about="The words under the value, or a list of them, a line each."),
        "detail_url": Option(about="The page the words under the value lead to."),
        "detail_tone": Option(about="A status tone the words under the value are colored by."),
        "tone": Option(about="A status tone the fact is colored by, for what it means."),
        "zero": Option(about="Its value is nothing counted, drawn quiet."),
        "icon": Option(about="The icon beside it."),
        "drawing": Option(
            about=(
                "What its value looks like, beside it: `Dots` for a part of a whole, `Trend` for a direction, "
                "or under it an `AllocationBar` with its legend."
            )
        ),
    },
    data=True,
    bare_with="value",
    shape=_attributes,
    public=True,
    example=(
        '{% facts %}{% fact "Version" icon="layers" %}<code>1.4.2</code>{% detail %}Tag only{% endfact %}{% endfacts %}'
    ),
)


def _options(part: Part, bits: list[str], parser) -> dict:
    given = token_kwargs(list(bits), parser)
    if len(given) != len(bits) or not part.admits(set(given)):
        raise template.TemplateSyntaxError(part.refused())
    return given


@register.tag(name="facts")
def compile_facts(parser, token) -> SlotsNode:
    given = _options(FACTS, token.split_contents()[1:], parser)
    if FACTS.bare_with in given:
        return SlotsNode(FACTS, {}, given)
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
    found = {name: template.NodeList() for name in FACT.parts}
    ahead = list(FACT.parts)
    while True:
        found[ahead.pop(0)] = parser.parse((*ahead, "endfact"))
        marker = parser.next_token().contents
        if marker == "endfact":
            return SlotsNode(FACT, found, given)
        ahead = ahead[ahead.index(marker) :]


# The template contract reads a tag's parameters from what it wraps.
compile_facts.__wrapped__ = FACTS  # type: ignore[attr-defined]  # a function's attribute, which typeshed does not declare
compile_fact.__wrapped__ = FACT  # type: ignore[attr-defined]  # as above
