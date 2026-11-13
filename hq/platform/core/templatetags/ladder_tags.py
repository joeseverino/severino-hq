"""The check ladder: checks read down one after another, each a rung that
holds, does not, or was not read, and opens to say why.

``{% check_ladder route=True %}{% check_rung state="holds" kind="Device" %}example-laptop{% more %}2 checks{% body %}...{% endcheck_rung %}{% endcheck_ladder %}``
draws ``partials/_check_ladder.html``, each rung ``partials/_check_rung.html``:
the connection panel's path to HQ, and inside each of its hops the checks
it rests on.
"""

from django import template

from hq.platform.application.page_parts import html_attributes
from hq.platform.core.templatetags.value_tags import slotted

register = template.Library()

# How a rung stands: its check holds, does not, or was not read.
STATES = ("holds", "does-not", "unknown")


def rung_frame(name: str, given: dict) -> dict:
    """What a rung's partial draws from what a page gave: its state, unknown
    unless given, and its disclosure's attributes, opened when ``open`` and
    each ``data_*`` option."""

    state = given.get("state") or "unknown"
    if state not in STATES:
        raise template.TemplateSyntaxError(f"{name} state= is one of {', '.join(STATES)}")
    return {
        "state": state,
        "kind": given.get("kind") or "",
        "evidence": given.get("evidence") or "",
        "attributes": html_attributes(
            {"open": bool(given.get("open"))}
            | {option.replace("_", "-"): value for option, value in given.items() if option.startswith("data_")}
        ),
    }


slotted("check_ladder", "partials/_check_ladder.html", ("content",), frozenset(("route",)), library=register)
slotted(
    "check_rung",
    "partials/_check_rung.html",
    ("content", "detail", "more", "page", "body"),
    frozenset(("state", "kind", "evidence", "open")),
    shape=rung_frame,
    library=register,
)
