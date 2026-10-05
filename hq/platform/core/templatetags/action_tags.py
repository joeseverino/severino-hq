"""Buttons that post on their own, from anywhere on a page.

A page never writes a form for one button. HQ renders one post form, once, at
the end of base.html, and ``post_button`` names it in the button's ``form``
attribute with the button's own ``formaction``. The button therefore works
inside another form without belonging to it: HTML has no nested forms, and a
form written inside another silently detaches every control after it.
"""

from django import template
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

register = template.Library()

POST_FORM_ID = "hq-post"


@register.filter
def row_value(item):
    """``{{ item|row_value }}``: what a queue row's own button names, from its one owner."""
    from hq.platform.application.action_items import row_value as named

    return named(item)


@register.filter
def decision(item):
    from hq.platform.application.decisions import decision as present

    return present(item)


@register.filter
def queue_entries(entries):
    """Keep the SDK attention include on the queue's single projection."""
    from hq.platform.application.dashboard import queue_item

    return [queue_item(entry.get("source_id", ""), entry["source"], entry["item"])
            for entry in entries]


@register.filter
def workflow_action(action):
    """An emitted ActionLink uses the same renderer as a page action."""
    from dataclasses import asdict, is_dataclass

    from hq.platform.application.pages import PageAction

    values = asdict(action) if is_dataclass(action) else action
    return PageAction(
        values["label"], values["url"], method=values["method"].lower(),
        primary=values.get("recommended", False),
        danger=values.get("effect") == "destructive", title=values.get("reason", ""),
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
    """The one control a narrow head keeps beside its title: the page's primary
    action, else its first that is not destructive, else its first."""

    actions = tuple(actions or ())
    return (
        next((action for action in actions if getattr(action, "primary", False)), None)
        or next((action for action in actions if not getattr(action, "danger", False)), None)
        or (actions[0] if actions else None)
    )


@register.filter
def as_ask(action):
    """An emitted ActionLink that asks for work, as the control that follows it:
    a row's Read now. It stands idle until pressed; the page's script follows
    what the press started."""
    from dataclasses import asdict, is_dataclass

    from hq.platform.application.asks import Ask

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
