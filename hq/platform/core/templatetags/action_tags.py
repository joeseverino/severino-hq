"""Buttons: the one tag that draws every control that looks like one, and the posts it sends from anywhere on a page.

A page never writes a form for one button. HQ renders one post form, once, at
the end of base.html, and ``button`` with ``post`` names it in the button's ``form``
attribute with the button's own ``formaction``. The button therefore works
inside another form without belonging to it: HTML has no nested forms, and a
form written inside another silently detaches every control after it.
"""

from typing import Annotated, Any
from urllib.parse import parse_qsl, urlsplit
lazy from dataclasses import asdict, is_dataclass

from django import template
from django.middleware.csrf import get_token
from django.template.loader import render_to_string
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from hq.platform.application.describe import Describe
from hq.platform.application.tone import Tone
lazy from hq.platform.application import action_items, asks, decisions
lazy from hq.platform.application.asks import Ask
lazy from hq.platform.application.capabilities import capability_words
lazy from hq.platform.application.dashboard import gathered_rows
lazy from hq.platform.application.page_parts import html_attributes
lazy from hq.platform.application.pages import PageAction, offered_now
lazy from hq.platform.application.ui import button_classes
lazy from hq.platform.core.template_parts import Part, slotted
lazy from hq.platform.core.templatetags.value_tags import (
    _request,
    arrow as onward_arrow,
    breakable,
    button_words,
    is_this_page,
)
lazy from hq.platform.core.templatetags.write_tags import write_token

register = template.Library()

POST_FORM_ID = "hq-post"


@register.filter
def row_value(item):
    """``{{ item|row_value }}``: what a queue row's own button names, from its one owner."""

    return action_items.row_value(item)


@register.simple_tag
def triage(item=None, *, sorts=True, family="", url=""):
    """``{% triage item sorts=triage as away %}``: the reader's own way to sort
    a queue row, Dismiss, or Restore once it is dismissed, posting the row's
    key; none where the queue is not the reader's to sort (``sorts``) or the
    row is not (``action_items.sortable``). With
    ``family`` and the part's dismiss ``url``, Dismiss of a whole family."""

    if not sorts or (item is not None and not action_items.sortable(item)):
        return None
    if family:
        # ``url`` is the part's own, whose query says which rows it shows.
        shown = dict(parse_qsl(urlsplit(url).query))
        return PageAction.command(action_items.SET_ASIDE, query={**shown, "family": family})
    command = action_items.RESTORE if item.get("aside") else action_items.SET_ASIDE
    return PageAction.command(command, field="key", value=action_items.row_value(item))


@register.filter
def families(items, fold):
    """``{{ items|families:fold }}``: the queue's entries, a family folded under its name when ``fold``."""

    return action_items.families(list(items or ()), fold=bool(fold))


@register.filter
def decision(item):
    return decisions.decision(item)


@register.filter
def urgency(status):
    """``{{ item.status|urgency }}``: how urgent, in the words every pill says."""

    return Tone(status).urgency


@register.filter
def queue_entries(entries):
    """Keep the SDK attention include on the queue's single projection."""

    return gathered_rows({"source_id": "", **entry} for entry in entries)


@register.filter
def workflow_action(action):
    """An emitted ActionLink uses the same renderer as a page action, so one
    the command would refuse now is drawn unusable, saying why."""

    return PageAction.of(action)


@register.simple_tag(takes_context=True)
def remedies(context, actions):
    """``{% remedies actions as shown %}``: the actions a page draws, each as a
    page action. A remedy is a way somewhere else, so one that leads only to
    the view it is drawn on is left out; a post still acts there. One its
    reader may not open is left out too (``pages.offered``). A box that holds
    remedies asks this first, so it is drawn only around a control."""

    shown = (workflow_action(action) for action in actions or ())
    return [
        action
        for action in shown
        if (action.method == "post" or not is_this_page(context, action.url)) and offered_now(action.url)
    ]


def post_button_markup(
    label, url, *, name="action", value="", css="btn", title="", disabled=False, pressed=None, hit=False, shared=True
):
    """The button ``button`` draws with ``post``, for Python that builds a page's
    markup: posting through the shared form, or the one form it stands in
    (``shared=False``)."""

    # A button of words is named by them, so its title only describes it; a
    # glyph (an arrow, a star) says nothing aloud and is named by its title.
    # A toggle (`pressed`, true or false) says aloud which way it stands. A
    # control drawn smaller than a target asks for its hit area (`hit`,
    # page.css).
    glyph = not any(character.isalnum() for character in str(label))
    attributes = {"name": name, "value": value, "title": title, "aria-label": title if glyph else ""}
    if pressed is not None:
        attributes["aria-pressed"] = "true" if pressed else "false"
    return format_html(
        '<button type="submit"{} class="{}"{}{}{}>{}</button>',
        format_html(' form="{}" formaction="{}"', POST_FORM_ID, url) if shared else "",
        css,
        format_html_join("", ' {}="{}"', ((key, val) for key, val in attributes.items() if val)),
        mark_safe(" data-hit") if hit else "",
        mark_safe(" disabled") if disabled else "",
        button_words(label),
    )


# What a button a script or the platform acts on may carry besides its
# ``data_*`` hooks: the popover it opens, the dialog command it sends, and
# hidden until a script shows it.
BUTTON_ATTRIBUTES = frozenset({"popovertarget", "command", "commandfor", "aria_haspopup", "hidden"})


def _button_words(label, icon="", count=None, onward=False):
    words = button_words(label)
    if onward:
        words = format_html("{}{}", words, mark_safe(onward_arrow()))  # noqa: S308 - the arrow tag's own markup
    if icon:
        # Stripping the part's markup makes it a plain string again.
        mark = mark_safe(render_to_string("partials/_icon.html", {"name": icon}).strip())  # noqa: S308 - the icon part's own markup
        words = format_html("{}{}", mark, words)
    if count not in (None, ""):
        words = format_html('{} <span class="filter-count">{}</span>', words, breakable(count))
    return words


def button(
    context,
    label: Annotated[Any, Describe("Its words.")],
    url: Annotated[Any, Describe("Where it leads, or with `post` where it posts.")] = "",
    *,
    post: Annotated[bool, Describe("Posts to its url.")] = False,
    submit: Annotated[bool, Describe("Sends its own form.")] = False,
    look: Annotated[str, Describe("How it looks, its words from `ui.BUTTON_LOOKS`.")] = "",
    css: Annotated[str, Describe("Classes in place of its look, for a control not drawn as a button.")] = "",
    title: Annotated[Any, Describe("Its `title`, read where it stands.")] = "",
    disabled: Annotated[bool, Describe("Neither a link nor a form, kept in the tab order.")] = False,
    current: Annotated[bool, Describe("It names the page being drawn.")] = False,
    name: Annotated[str, Describe("The field it sends.")] = "",
    value: Annotated[Any, Describe("That field's value.")] = "",
    pressed: Annotated[bool | None, Describe("A toggle, and which way it stands.")] = None,
    hit: Annotated[bool, Describe("Drawn smaller than a target, so it asks for its hit area.")] = False,
    fields: Annotated[Any, Describe("What a posting one sends besides its own field, as a form of its own.")] = None,
    modal: Annotated[str, Describe("The dialog a link opens where script runs.")] = "",
    icon: Annotated[str, Describe("The icon its words open with.")] = "",
    count: Annotated[Any, Describe("A count of what it holds, after its words.")] = None,
    arrow: Annotated[bool, Describe("Ends with the arrow of a link onward.")] = False,
    **attributes: Annotated[
        Any, Describe("Each `data_*` hook, and the popover, dialog command or `hidden` a script acts on.")
    ],
):
    """Every control that looks like a button, and the only thing that writes
    one: a link to ``url``; with ``post``, one that posts to it; with
    ``submit``, its form's own; with neither and no ``url``, one a script or
    the platform acts on (``data_*``, ``BUTTON_ATTRIBUTES``). Its words may
    open with an ``icon``, close with a ``count`` of what it holds, or end
    with the ``arrow`` of a link onward. ``look`` is how
    it looks (``ui.BUTTON_LOOKS``); ``css`` stands in for the button's look
    on a control that is not drawn as one (an icon, a pin). Disabled or the
    page it names (``current``), it is neither a link nor a form, and stays in
    the tab order so its title is read where it stands. A posting one's
    ``fields``, what it sends besides its own field, make it a form of its
    own, since the shared form carries only the button's field; it then
    returns to the page it was pressed on unless they name a ``next``. Its
    token is the request's, which an ``{% include ... only %}`` keeps when it
    drops the context processors' values."""

    if css and look:
        raise template.TemplateSyntaxError("button takes look= or css=, not both")
    if post + submit + bool(current) > 1 or (post and not url) or ((submit or current) and url):
        raise template.TemplateSyntaxError(
            "button posts to a url (post=True), sends its form (submit=True), names this page (current=True), "
            "or links to a url"
        )
    # A posting button is drawn by `post_button_markup`, which carries no hooks.
    unknown = sorted(
        key for key in attributes if post or (not key.startswith("data_") and key not in BUTTON_ATTRIBUTES)
    )
    if unknown:
        raise template.TemplateSyntaxError(f"button takes no {', '.join(unknown)}")
    try:
        classes = css or button_classes(look)
    except ValueError as error:
        raise template.TemplateSyntaxError(str(error)) from error
    if post:
        options = {"name": name or "action", "value": value, "css": classes, "title": title}
        options |= {"disabled": disabled, "pressed": pressed}
        if not fields:
            return post_button_markup(label, url, hit=hit, **options)
        request = _request(context)
        sent = {"next": request.get_full_path(), **fields}
        with context.push(csrf_token=get_token(request)):
            token = write_token(context)
        return (
            format_html('<form method="post" action="{}" class="post-form">', url)
            + token
            + format_html_join("", '<input type="hidden" name="{}" value="{}">', sent.items())
            + post_button_markup(label, url, hit=hit, shared=False, **options)
            + mark_safe("</form>")
        )
    words = _button_words(label, icon, count, arrow)
    named = {"class": classes, "title": title} | {key.replace("_", "-"): val for key, val in attributes.items()}
    if (disabled and not submit) or current:
        state = {"role": "button", "aria-disabled": "true", "tabindex": "0"} if disabled else {"aria-current": "page"}
        return format_html("<span{}>{}</span>", html_attributes(named | state), words)
    if url:
        return format_html("<a{}>{}</a>", html_attributes({"href": url, "data-modal-open": modal} | named), words)
    kind = {"type": "submit" if submit else "button", "name": name, "value": value, "disabled": bool(disabled)}
    return format_html("<button{}>{}</button>", html_attributes(kind | named), words)


# How many controls a head draws beside its title; the rest wait in its
# menu. A page names more (``page_beside``) where a decision takes several
# controls side by side, up to ``MOST_BESIDE``.
BESIDE_THE_TITLE = 2
MOST_BESIDE = 4


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


@register.simple_tag(takes_context=True)
def head_split(context, actions):
    """``{% head_split page.actions as split %}``: ``head_actions`` with as
    many beside the title as the page names (``page_beside``), two where it
    names none."""

    return head_actions(actions, context.get("page_beside", ""))


def head_actions(actions, beside=""):
    """``(beside, copies, rest)``: the controls a head draws beside its
    title, the first it reaches for (``BESIDE_THE_TITLE``, or ``beside`` where
    the page names more); the menu's copies of each of those but the lead;
    and every other control, for the menu. The stylesheet shows the copies
    only where the head is too narrow for the controls beside its title, and
    folds those in."""

    most = int(beside) if beside else BESIDE_THE_TITLE
    if not BESIDE_THE_TITLE <= most <= MOST_BESIDE:
        raise ValueError(f"A head keeps {BESIDE_THE_TITLE} to {MOST_BESIDE} controls beside its title, not {most}.")
    actions = tuple(actions or ())
    shown = tuple(_ranked(actions)[:most])
    if not shown:
        return (), (), ()
    rest = tuple(action for action in actions if all(action is not drawn for drawn in shown))
    return shown, shown[1:], rest


@register.filter
def as_ask(action):
    """An emitted ActionLink that asks for work, as the control that follows it:
    a row's Refresh. It stands idle until pressed; the page's script follows
    what the press started."""

    values = asdict(action) if is_dataclass(action) else action
    return Ask(values["label"], values["url"], title=values.get("reason", ""), compact=True)


def words(
    capability: Annotated[Any, Describe("The command, by name.")],
    variant: Annotated[str, Describe("Which of its words, where it has more than one.")] = "",
):
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
def refreshed():
    """``{% refreshed %}``: what a reading says once it is in."""

    return asks.REFRESHED


@register.simple_tag
def newer_reading():
    """``{% newer_reading %}``: what a page says of a newer reading it could
    not draw in place, before the link that loads it."""

    return asks.NEWER_READING


@register.simple_tag
def show_newer():
    """``{% show_newer %}``: the link that loads a newer reading."""

    return asks.SHOW_NEWER


@register.simple_tag
def ask_button(ask, extra=""):
    """The button of an ``application.asks.Ask``: a posting ``button`` the page's
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
        button_classes(f"{ask.look} {extra}"),
        format_html_join("", ' {}="{}"', ((key, val) for key, val in attributes.items() if val)),
        mark_safe(" disabled") if ask.disabled else "",
        button_words(ask.label),
    )


# A page that helps look into a problem, drawn as its mark: the read-only
# links every finding carries (application.action_links), by their name.
_LINK_ICONS = {"topology": "network", "impact": "layers"}


@register.filter
def link_icon(link):
    """The icon a card draws for ``link`` in place of its words, or ""."""
    name = link.get("name", "") if isinstance(link, dict) else getattr(link, "name", "")
    return _LINK_ICONS.get(name, "")


slotted(
    Part(
        name="button",
        about=(
            "Every control that looks like a button: a link, a post through the page's one shared form, its "
            "form's own, or one a script acts on."
        ),
        function=button,
        public=True,
        example='{% button "Restart" url post=True look="primary" %}',
    ),
    library=register,
)
slotted(
    Part(
        name="words",
        about="What a button for a command says, from its declaration, for prose that names the one to press.",
        function=words,
        public=True,
        example='Press {% words "agent.access.set" %}.',
    ),
    library=register,
)
