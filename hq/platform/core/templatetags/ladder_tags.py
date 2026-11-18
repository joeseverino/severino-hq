"""The check ladder: checks read down one after another, each a rung that
is met, unmet or unavailable (``standards.Standing``), and opens to say why:
the connection panel's path to HQ, and inside each of its hops the checks it
rests on."""

from django import template

from hq.platform.application.page_parts import html_attributes
from hq.platform.application.standards import Standing
from hq.platform.core.template_parts import Option, Part, slotted
from hq.platform.core.templatetags.status_tags import tone_class

register = template.Library()


def rung_frame(_name: str, given: dict) -> dict:
    """What a rung's partial draws from what a page gave: its standing,
    unavailable unless given, and its disclosure's attributes, opened when
    ``open`` and each ``data_*`` option."""

    state = Standing(given["state"] or Standing.UNAVAILABLE)
    return {
        "state": state,
        "tone": tone_class(state.tone),
        "kind": given["kind"] or "",
        "evidence": given["evidence"] or "",
        "attributes": html_attributes(
            {"open": bool(given["open"])}
            | {option.replace("_", "-"): value for option, value in given.items() if option.startswith("data_")}
        ),
    }


slotted(
    Part(
        name="check_ladder",
        example='{% check_ladder route=True %}{% check_rung state="met" evidence="192.0.2.10" %}Example address is known{% body %}<p>Example HQ knows the address.</p>{% endcheck_rung %}{% endcheck_ladder %}',
        classes=("check-ladder", "is-route"),
        partial="partials/_check_ladder.html",
        about="Checks read down one after another, each a `{% check_rung %}` opening to say why it stands as it does.",
        options={
            "route": Option(about="The stops of a route, from where a request starts to where it ends, on one line.")
        },
    ),
    library=register,
)
slotted(
    Part(
        name="check_rung",
        example='{% check_ladder %}{% check_rung state="unmet" kind="Tailnet" %}Example policy admits the device{% body %}<p>Example HQ could not read the policy.</p>{% endcheck_rung %}{% endcheck_ladder %}',
        classes=(
            *("ladder-body", "ladder-check", "ladder-detail", "ladder-evidence", "ladder-kind", "ladder-mark"),
            *("ladder-more", "ladder-page", "ladder-rung", "ladder-statement"),
        ),
        partial="partials/_check_rung.html",
        about=(
            "One rung of a `{% check_ladder %}`: its tick, its statement with any `detail` part, and its `more` "
            "part saying what opening it shows; it opens to its `body` part, after the `page` part's link to "
            "what it is about."
        ),
        parts=("content", "detail", "more", "page", "body"),
        options={
            "state": Option(choices=("", *Standing), about="How its check stands, unavailable when not read."),
            "kind": Option(about="What kind of check it is."),
            "evidence": Option(about="What it was read from."),
            "open": Option(about="Drawn opened."),
        },
        data=True,
        shape=rung_frame,
    ),
    library=register,
)
