"""Buttons that post on their own, from anywhere on a page.

A page never writes a form for one button. HQ renders one post form, once, at
the end of base.html, and ``post_button`` names it in the button's ``form``
attribute with the button's own ``formaction``. The button therefore works
inside another form without belonging to it: HTML has no nested forms, and a
form written inside another silently detaches every control after it.
"""

lazy from dataclasses import asdict, is_dataclass

from django import template
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

lazy from hq.platform.application import action_items, decisions
lazy from hq.platform.application.asks import Ask
lazy from hq.platform.application.dashboard import queue_item
lazy from hq.platform.application.pages import PageAction

register = template.Library()

POST_FORM_ID = "hq-post"


@register.filter
def row_value(item):
    """``{{ item|row_value }}``: what a queue row's own button names, from its one owner."""

    return action_items.row_value(item)


@register.filter
def decision(item):
    return decisions.decision(item)


@register.filter
def urgency(status):
    """``{{ item.status|urgency }}``: how urgent, in the words every pill says."""

    return decisions.urgency(status)


@register.filter
def workflow_parts(plan):
    """A plan's steps and its reason, apart, as a card shows them."""

    return decisions.workflow_parts(plan)


@register.filter
def queue_entries(entries):
    """Keep the SDK attention include on the queue's single projection."""

    return [queue_item(entry.get("source_id", ""), entry["source"], entry["item"]) for entry in entries]


@register.filter
def workflow_action(action):
    """An emitted ActionLink uses the same renderer as a page action."""

    values = asdict(action) if is_dataclass(action) else action
    return PageAction(
        values["label"],
        values["url"],
        method=values["method"].lower(),
        primary=values.get("recommended", False),
        danger=values.get("effect") == "destructive",
        title=values.get("reason", ""),
    )


@register.simple_tag
def post_button(label, url, *, name="action", value="", css="btn", title="", disabled=False):
    attributes = {"name": name, "value": value, "title": title, "aria-label": title}
    return format_html(
        '<button type="submit" form="{}" formaction="{}" class="{}"{}{}>{}</button>',
        POST_FORM_ID,
        url,
        css,
        format_html_join("", ' {}="{}"', ((key, val) for key, val in attributes.items() if val)),
        mark_safe(" disabled") if disabled else "",
        label,
    )


@register.filter
def lead_action(actions):
    """The one control a narrow head keeps beside its title, chosen by what each
    is for rather than where it stands: never a destructive one while another
    is offered, then one that can be pressed, then the page's primary, then
    the first. So a service's Restart leads and its Stop waits in the menu."""

    actions = tuple(actions or ())
    if not actions:
        return None
    ranked = sorted(
        enumerate(actions),
        key=lambda pair: (
            bool(getattr(pair[1], "danger", False)),
            bool(getattr(pair[1], "disabled", False)),
            not getattr(pair[1], "primary", False),
            pair[0],
        ),
    )
    return ranked[0][1]


@register.filter
def as_ask(action):
    """An emitted ActionLink that asks for work, as the control that follows it:
    a row's Read now. It stands idle until pressed; the page's script follows
    what the press started."""

    values = asdict(action) if is_dataclass(action) else action
    return Ask(values["label"], values["url"], title=values.get("reason", ""), compact=True)


@register.simple_tag
def ask_button(ask, extra=""):
    """The button of an ``application.asks.Ask``: a ``post_button`` the page's
    script may answer in place. Busy is ``aria-disabled``, which keeps focus."""

    attributes = {
        "name": ask.name,
        "value": ask.value,
        "title": ask.title,
        "aria-disabled": "true" if ask.standing.live else "",
    }
    return format_html(
        '<button type="submit" form="{}" formaction="{}" class="{}" data-ask-button{}{}>{}</button>',
        POST_FORM_ID,
        ask.url,
        " ".join(part for part in (ask.css, extra) if part),
        format_html_join("", ' {}="{}"', ((key, val) for key, val in attributes.items() if val)),
        mark_safe(" disabled") if ask.disabled else "",
        ask.label,
    )


# A page that helps look into a problem, drawn as its mark: the read-only
# links every finding carries (application.action_links), by their name.
_LINK_ICONS = {"topology": "network", "impact": "layers"}


@register.filter
def link_icon(link):
    """The icon a card draws for ``link`` in place of its words, or ""."""
    name = link.get("name", "") if isinstance(link, dict) else getattr(link, "name", "")
    return _LINK_ICONS.get(name, "")
