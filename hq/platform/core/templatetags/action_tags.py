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
from django.utils.safestring import mark_safe

from hq.platform.application.describe import Describe
from hq.platform.application.tone import Tone
from hq.platform.core.held_values import format_html, format_html_join
from hq.platform.core.part_examples import tags
lazy from hq.platform.application import action_items, asks, decisions
lazy from hq.platform.application.asks import Ask
lazy from hq.platform.application.capabilities import capability_words
lazy from hq.platform.application.dashboard import gathered_rows
lazy from hq.platform.application.page_parts import html_attributes
lazy from hq.platform.application.pages import PageAction, offered_now
lazy from hq.platform.application.ui import button_classes
lazy from hq.platform.core.template_parts import Example, Part, answers, slotted
lazy from hq.platform.core.templatetags.value_tags import (
    OUTSIDE_LINK,
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


@answers(register)
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
    """The entries ``plugin_attention_items`` gathers as the queue's rows."""

    return gathered_rows(entries)


@register.filter
def workflow_action(action):
    """An emitted ActionLink uses the same renderer as a page action, so one
    the command would refuse now is drawn unusable, saying why."""

    return PageAction.of(action)


@answers(register, takes_context=True)
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


def offer_markup(offer, *, look=""):
    """The button ``button`` draws for ``offer``, for Python that builds a
    page's markup."""

    return _posting(offer.label, offer, classes=button_classes(look or offer.look))


def _posting(label, offer, *, classes, title="", disabled=False, pressed=None, hit=False, context=None):
    """The one way HQ writes a button that posts: what ``offer`` sends, to
    where it sends it, through the shared form, or as a form of its own where
    it sends ``fields`` besides its own field. The shared form carries only
    the button's field; a form of its own returns to the page it was pressed
    on unless its fields name a ``next``."""

    if not isinstance(offer, PageAction) or offer.method != "post":
        raise template.TemplateSyntaxError(
            "A button posts only what an offer sends: PageAction.command, or a RowOrder's or RowMark's press."
        )
    button = _post_button(
        label,
        offer.url,
        name=offer.field,
        value=offer.value,
        css=classes,
        title=title or offer.title,
        disabled=disabled or offer.disabled,
        pressed=pressed,
        hit=hit,
        shared=not offer.fields,
    )
    if not offer.fields:
        return button
    if context is None:
        raise ValueError(f"{offer.label} sends fields of its own, so it is drawn in a template, as a form.")
    request = _request(context)
    sent = {"next": request.get_full_path(), **offer.fields}
    with context.push(csrf_token=get_token(request)):
        token = write_token(context)
    return (
        format_html('<form method="post" action="{}" class="post-form">', offer.url)
        + token
        + format_html_join("", '<input type="hidden" name="{}" value="{}">', sent.items())
        + button
        + mark_safe("</form>")
    )


def _post_button(label, url, *, name, value, css, title, disabled, pressed, hit, shared):
    # A button of words is named by them, so its title only describes it; a
    # glyph (an arrow, a star) says nothing aloud and is named by its title.
    # A toggle (`pressed`, true or false) says aloud which way it stands. A
    # control drawn smaller than a target asks for its hit area (`hit`,
    # page.css).
    attributes = {"name": name, "value": value, "title": title, "aria-label": title if _glyph(label) else ""}
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


def _glyph(label):
    """Whether ``label`` is a glyph (an arrow, a star), which says nothing aloud."""

    return not any(character.isalnum() for character in str(label))


def _button_words(label, icon="", count=None, onward=False, *, unseen=False):
    # A glyph button with an icon shows the icon alone; its words name it.
    words = button_words(label)
    if unseen and icon:
        words = format_html('<span class="visually-hidden">{}</span>', words)
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
    url: Annotated[Any, Describe("Where it leads.")] = "",
    *,
    outside: Annotated[bool, Describe("Its url is a page outside HQ, opened as every outside link is.")] = False,
    offer: Annotated[Any, Describe("A `PageAction` that posts, which it sends: where, its field and the rest.")] = None,
    submit: Annotated[bool, Describe("Sends its own form.")] = False,
    look: Annotated[str, Describe("How it looks, its words from `ui.BUTTON_LOOKS`.")] = "",
    css: Annotated[str, Describe("Classes in place of its look, for a control not drawn as a button.")] = "",
    title: Annotated[Any, Describe("Its `title`, read where it stands.")] = "",
    disabled: Annotated[bool, Describe("Neither a link nor a form, kept in the tab order.")] = False,
    current: Annotated[bool, Describe("It names the page being drawn.")] = False,
    name: Annotated[str, Describe("The field its form's own sends.")] = "",
    value: Annotated[Any, Describe("That field's value.")] = "",
    pressed: Annotated[bool | None, Describe("A toggle, and which way it stands.")] = None,
    checked: Annotated[
        bool | None, Describe("A switch, its track drawn after its words, and which way it stands.")
    ] = None,
    expanded: Annotated[bool | None, Describe("Shows or hides what it governs, and which it does now.")] = None,
    heard: Annotated[Any, Describe("Its name aloud, where its words are a glyph or none and it has no title.")] = "",
    hit: Annotated[bool, Describe("Drawn smaller than a target, so it asks for its hit area.")] = False,
    modal: Annotated[str, Describe("The dialog a link opens where script runs.")] = "",
    icon: Annotated[str, Describe("The icon its words open with.")] = "",
    count: Annotated[Any, Describe("A count of what it holds, after its words.")] = None,
    arrow: Annotated[bool, Describe("Ends with the arrow of a link onward.")] = False,
    **attributes: Annotated[
        Any, Describe("Each `data_*` hook, and the popover, dialog command or `hidden` a script acts on.")
    ],
):
    """Every control that looks like a button, and the only thing that writes one.

    A link to ``url``; with an ``offer``, one that posts; with ``submit``, its
    form's own; with neither, one a script acts on (``data_*``). Disabled or
    ``current``, it stays in the tab order so its title is read. A page posts
    only through an offer, built where the rule that would refuse the press is
    read, so no page shows a press its reader may not make.
    """

    posts = offer is not None
    if css and look:
        raise template.TemplateSyntaxError("button takes look= or css=, not both")
    if posts + submit + bool(current) > 1 or ((posts or submit or current) and url):
        raise template.TemplateSyntaxError(
            "button posts an offer (offer=), sends its form (submit=True), names this page (current=True), "
            "or links to a url"
        )
    # A posting button sends what its offer says, and carries no hooks.
    unknown = sorted(
        key for key in attributes if posts or (not key.startswith("data_") and key not in BUTTON_ATTRIBUTES)
    )
    if posts and (name or value != ""):
        unknown += ["name", "value"]
    if outside and (not url or attributes or modal):
        raise template.TemplateSyntaxError("button leads outside (outside=True) to a url, with no hooks or modal")
    if (posts or url or current) and (checked is not None or expanded is not None or heard):
        unknown += ["checked", "expanded", "heard"]
    if unknown:
        raise template.TemplateSyntaxError(f"button takes no {', '.join(unknown)}")
    try:
        classes = css or button_classes(look)
    except ValueError as error:
        raise template.TemplateSyntaxError(str(error)) from error
    if posts:
        return _posting(
            label, offer, classes=classes, title=title, disabled=disabled, pressed=pressed, hit=hit, context=context
        )
    words = _button_words(label, icon, count, arrow, unseen="glyph" in look.split())
    named = {"class": classes, "title": title} | {key.replace("_", "-"): val for key, val in attributes.items()}
    if (disabled and not submit) or current:
        state = {"role": "button", "aria-disabled": "true", "tabindex": "0"} if disabled else {"aria-current": "page"}
        return format_html("<span{}>{}</span>", html_attributes(named | state), words)
    if outside:
        named = {"css": classes, "title": title, "label": title if _glyph(label) else "", "content": words}
        return mark_safe(render_to_string(OUTSIDE_LINK, {"url": url, **named}).strip())  # noqa: S308 - the outside link part's own markup
    if url:
        return format_html("<a{}>{}</a>", html_attributes({"href": url, "data-modal-open": modal} | named), words)
    # A glyph or no words is named by its title, as a posting button is.
    stands = {
        "role": "switch" if checked is not None else "",
        "aria-checked": _way(checked),
        "aria-pressed": _way(pressed),
        "aria-expanded": _way(expanded),
        "aria-label": heard or (title if _glyph(label) else ""),
    }
    if checked is not None:
        words = format_html("<span>{}</span>{}", words, mark_safe(SWITCH_TRACK))  # noqa: S308 - the switch's own literal markup
    kind = {"type": "submit" if submit else "button", "name": name, "value": value, "disabled": bool(disabled)}
    return format_html("<button{}>{}</button>", html_attributes(kind | named | stands), words)


SWITCH_TRACK = '<span class="switch-track" aria-hidden="true"><span class="switch-knob"></span></span>'


def _way(state):
    """A state ARIA reads as ``"true"`` or ``"false"``, or none where it is not given."""

    return "" if state is None else ("true" if state else "false")


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


@answers(register, takes_context=True)
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


@answers(register)
def waiting():
    """``{% waiting %}``: what asked-for work says while it waits its turn."""

    return asks.WAITING


@answers(register)
def refreshing():
    """``{% refreshing %}``: what a reading says while it is fetched again."""

    return asks.REFRESHING


@answers(register)
def newer_reading():
    """``{% newer_reading %}``: what a page says of a newer reading it could
    not draw in place, before the link that loads it."""

    return asks.NEWER_READING


@answers(register)
def show_newer():
    """``{% show_newer %}``: the link that loads a newer reading."""

    return asks.SHOW_NEWER


def ask_button(
    ask: Annotated[Any, Describe("The `application.asks.Ask` it asks for.")],
    extra: Annotated[str, Describe("Looks it takes beside the ask's own.")] = "",
):
    """The button of an ask (its Part's rules)."""

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
        rules="""
        Primary for the one thing a page is for; default for the rest; `ghost` for link-like actions that
        sit inline; `danger` in red text on the same surface as its neighbors, loud only in its
        confirmation; `compact` for one that stands in a row, a list or a head, a line tall; `glyph` for a
        square that holds a glyph or an `icon` alone (a row's order and mark, a page's links on a queue
        row with `ghost`), its words heard and unseen; `link` for one among words, drawn as a link; `menu`
        for a row of a menu; `bar` for a control of the site header's bar, on the bar's ground. `link` and
        `menu` draw no box, so `button_classes` drops a look that paints one beside them, and a
        destructive row keeps only its red.

        Every control that looks like a button is `{% button label url look="primary compact" %}`, the
        one writer of `.btn` (`ui.button_classes` for Python's parts): a link, with `outside=True` one out
        of HQ drawn as `{% outside_link %}` draws it, with an `offer` one that posts, with `submit=True`
        its form's own, and with neither and no url one a script acts on (`data_*`, `popovertarget`,
        `command`). Its words may open with an `icon`, close with a `count` or end with an `arrow`; one with
        a glyph or no words is named aloud by its `heard`, else its title. A `<button>` may be a toggle
        (`pressed`), a switch drawn with its track (`checked`) or a disclosure (`expanded`). A post
        is only ever an offer's: a `PageAction` from `PageAction.command`, which is None for a reader who
        may not run the command, or a `RowOrder`'s or `RowMark`'s press, so a page has no way to draw a
        press its reader would be refused. `_page_action.html` draws one; a confirmation's is
        `PageAction.command(..., confirms=True)`, loud where it destroys. A toggle that posts is a button
        with `post` and `pressed`; the calendar's sources are a list of them, each in its series color
        (`partials/_calendar_sources.html`).

        The push button is `.btn`, and a `<button>` with no class of its own; a button with a class (a
        menu row, a toggle, a close mark) starts from nothing painted (`base.css`) and draws itself, so no
        rule takes the push button's look back off. A label keeps its line, from the button rule alone
        (`page.css`); only a tight table lets one wrap. A label too long for a phone is shortened where it
        is declared. A control drawn smaller than a target asks for its area with `hit=True`. *Gate:*
        `test_a_controls_label_keeps_its_line_from_the_button_rule_alone`, and the
        `action-intent-names-no-value` ast-grep rule.
        """,
        about=(
            "Every control that looks like a button: a link, a post through the page's one shared form, its "
            "form's own, or one a script acts on."
        ),
        function=button,
        public=True,
        example=tags.BUTTON,
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
slotted(
    Part(
        name="ask_button",
        about="The button of an ask: a posting `button` the page's script may answer in place.",
        rules="""
        An ask's button is drawn by `partials/_ask.html` alone, around its note. While its work is live it
        is `aria-disabled`, which keeps its focus, and its title says what the work said, where a cell
        shows only its word.
        """,
        function=ask_button,
        example=Example(
            "{% ask_button ask %}",
            lambda: {"ask": Ask("Read example", "/example/read/", title="Read the example again.")},
        ),
    ),
    library=register,
)
