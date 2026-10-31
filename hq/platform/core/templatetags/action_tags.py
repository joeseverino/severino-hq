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

lazy from hq.platform.application import action_items, asks, decisions
lazy from hq.platform.application.asks import Ask
lazy from hq.platform.application.capabilities import capability_words
lazy from hq.platform.application.dashboard import queue_item
lazy from hq.platform.application.pages import PageAction
lazy from hq.platform.core.templatetags.value_tags import breakable, is_this_page

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
    """An emitted ActionLink uses the same renderer as a page action, so one
    the command would refuse now is drawn unusable, saying why."""

    if isinstance(action, PageAction):
        return action
    values = asdict(action) if is_dataclass(action) else action
    refusal = values.get("refusal", "")
    return PageAction(
        values["label"],
        values["url"],
        method=values["method"].lower(),
        primary=values.get("recommended", False),
        danger=values.get("effect") == "destructive",
        title=refusal or values.get("reason", ""),
        disabled=bool(refusal),
    )


@register.simple_tag(takes_context=True)
def remedies(context, actions):
    """``{% remedies actions as shown %}``: the actions a page draws, each as a
    page action. A remedy is a way somewhere else, so one that leads only to
    the view it is drawn on is left out; a post still acts there. A box that
    holds remedies asks this first, so it is drawn only around a control."""

    shown = (workflow_action(action) for action in actions or ())
    return [action for action in shown if action.method == "post" or not is_this_page(context, action.url)]


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
        breakable(label),
    )


# How many controls a head draws beside its title; the rest wait in its menu.
BESIDE_THE_TITLE = 2


def _ranked(actions):
    """The controls in the order a head reaches for them, by what each is for
    rather than where it stands: never a destructive one while another is
    offered, then one that can be pressed, then the page's primary, then the
    page's own order. So a service's Restart leads and its Stop waits in the
    menu."""

    return [
        action
        for _at, action in sorted(
            enumerate(actions),
            key=lambda pair: (pair[1].danger, pair[1].disabled, not pair[1].primary, pair[0]),
        )
    ]


@register.filter
def lead_action(actions):
    """The control a head draws first beside its title."""

    ranked = _ranked(tuple(actions or ()))
    return ranked[0] if ranked else None


@register.filter
def head_actions(actions):
    """``(beside, menu)``: the controls a head draws beside its title, the
    first two it reaches for, and every one but the lead for its menu, the
    second first. The stylesheet leaves out the menu's copy of a control drawn
    beside the title, and folds the second in where the head is narrow."""

    actions = tuple(actions or ())
    beside = tuple(_ranked(actions)[:BESIDE_THE_TITLE])
    if not beside:
        return (), ()
    rest = tuple(action for action in actions if all(action is not shown for shown in beside))
    return beside, (*beside[1:], *rest)


@register.filter
def as_ask(action):
    """An emitted ActionLink that asks for work, as the control that follows it:
    a row's Refresh. It stands idle until pressed; the page's script follows
    what the press started."""

    values = asdict(action) if is_dataclass(action) else action
    return Ask(values["label"], values["url"], title=values.get("reason", ""), compact=True)


@register.simple_tag
def words(capability, variant=""):
    """``{% words "name" %}``: what a button for that command says, from its
    declaration, for prose that tells a person which one to press."""

    return capability_words(capability, variant)


@register.simple_tag
def waiting():
    """``{% waiting %}``: what asked-for work says while it waits its turn."""

    return asks.WAITING


@register.simple_tag
def refreshing():
    """``{% refreshing %}``: what a reading says while it is fetched again."""

    return asks.REFRESHING


@register.simple_tag
def applies_shortly():
    """``{% applies_shortly %}``: what a saved change says of when it goes live."""

    return asks.APPLIES_SHORTLY


@register.simple_tag
def ask_button(ask, extra=""):
    """The button of an ``application.asks.Ask``: a ``post_button`` the page's
    script may answer in place. Busy is ``aria-disabled``, which keeps focus."""

    attributes = {
        "name": ask.name,
        "value": ask.value,
        # What the work said, where a cell shows only its word (_ask.html).
        "title": ask.standing.note if ask.standing.state != asks.IDLE and ask.standing.note else ask.title,
        # What the button is for, which the title says again once the work
        # has nothing to say (ask.js).
        "data-ask-reason": ask.title,
        "aria-disabled": "true" if ask.standing.live else "",
    }
    return format_html(
        '<button type="submit" form="{}" formaction="{}" class="{}" data-ask-button{}{}>{}</button>',
        POST_FORM_ID,
        ask.url,
        " ".join(part for part in (ask.css, extra) if part),
        format_html_join("", ' {}="{}"', ((key, val) for key, val in attributes.items() if val)),
        mark_safe(" disabled") if ask.disabled else "",
        breakable(ask.label),
    )


# A page that helps look into a problem, drawn as its mark: the read-only
# links every finding carries (application.action_links), by their name.
_LINK_ICONS = {"topology": "network", "impact": "layers"}


@register.filter
def link_icon(link):
    """The icon a card draws for ``link`` in place of its words, or ""."""
    name = link.get("name", "") if isinstance(link, dict) else getattr(link, "name", "")
    return _LINK_ICONS.get(name, "")
