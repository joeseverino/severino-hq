"""The check ladder: checks read down one after another, each a rung that
is met, unmet or unavailable (``standards.Standing``), and opens to say why.

``{% check_ladder route=True %}{% check_rung state="met" kind="Device" %}example-laptop{% more %}2 checks{% body %}...{% endcheck_rung %}{% endcheck_ladder %}``
draws ``partials/_check_ladder.html``, each rung ``partials/_check_rung.html``:
the connection panel's path to HQ, and inside each of its hops the checks
it rests on.
"""

from django import template

from hq.platform.application.page_parts import html_attributes
from hq.platform.application.standards import Standing
from hq.platform.core.templatetags.value_tags import Option, Part, slotted

register = template.Library()


def rung_frame(name: str, given: dict) -> dict:
    """What a rung's partial draws from what a page gave: its standing,
    unavailable unless given, and its disclosure's attributes, opened when
    ``open`` and each ``data_*`` option."""

    state = given.get("state") or Standing.UNAVAILABLE
    if state not in Standing:
        raise template.TemplateSyntaxError(f"{name} state= is one of {', '.join(Standing)}")
    return {
        "state": state,
        "kind": given.get("kind") or "",
        "evidence": given.get("evidence") or "",
        "attributes": html_attributes(
            {"open": bool(given.get("open"))}
            | {option.replace("_", "-"): value for option, value in given.items() if option.startswith("data_")}
        ),
    }


slotted(Part(name="check_ladder", partial="partials/_check_ladder.html", options={"route": Option()}), library=register)
slotted(
    Part(
        name="check_rung",
        partial="partials/_check_rung.html",
        parts=("content", "detail", "more", "page", "body"),
        options={"state": Option(), "kind": Option(), "evidence": Option(), "open": Option()},
        data=True,
        shape=rung_frame,
    ),
    library=register,
)
