"""Facts: every label and its value a page shows, drawn by one part.

``{% facts layout="band" %}{% fact "Running" value=n url=u detail=d tone=t icon="container" %}{% endfacts %}``
draws ``partials/_facts.html`` around each ``partials/_fact.html``. A band is
the few facts that sum up a page or a part, each a cell of its own; a list
(the default) is the facts about one thing, each label beside its value. A
fact whose value is markup is written as a block, its detail after
``{% detail %}``:
``{% fact "Version" icon="layers" %}<code>{{ tag }}</code>{% detail %}<small>Tag only</small>{% endfact %}``.
Given ``value=``, a fact is one tag with no end.
"""

import inspect

from django import template
from django.template.base import token_kwargs

from hq.platform.core.templatetags.value_tags import SlotsNode, Takes

register = template.Library()

FACTS_PARTIAL = "partials/_facts.html"
FACT_PARTIAL = "partials/_fact.html"

# Each layout, drawn as its class (``facts-band``) by the partial.
LAYOUTS = ("list", "band")


class FactsTakes(Takes):
    """What ``{% facts %}`` takes, and the layout it draws."""

    def values(self, given: dict) -> dict:
        layout = given.get("layout") or "list"
        if layout not in LAYOUTS:
            raise template.TemplateSyntaxError(f"facts layout= is one of {', '.join(sorted(LAYOUTS))}")
        return {"layout": layout, "label": given.get("label") or ""}


class FactTakes(Takes):
    """What ``{% fact %}`` takes: its label first, then its options. Given
    ``value``, the option ``bare_with`` names, it encloses nothing."""

    bare_with = "value"

    def __init__(self) -> None:
        options = frozenset(("value", "url", "detail", "tone", "icon"))
        super().__init__("fact", FACT_PARTIAL, ("value", "detail"), options, None)
        ordered, keyword = inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY
        self.__signature__ = inspect.Signature(
            [
                inspect.Parameter("content", ordered),
                inspect.Parameter("label", ordered),
                *(inspect.Parameter(option, keyword, default="") for option in sorted(options)),
            ]
        )


FACTS = FactsTakes("facts", FACTS_PARTIAL, ("content",), frozenset(("layout", "label")), None)
FACT = FactTakes()


def _options(takes: Takes, bits: list[str], parser) -> dict:
    given = token_kwargs(list(bits), parser)
    if len(given) != len(bits) or not takes.admits(set(given)):
        raise template.TemplateSyntaxError(f"{takes.name} takes {', '.join(sorted(takes.options))} as name=value")
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
