import inspect
import re
from collections.abc import Callable
from datetime import date, datetime
from numbers import Number
from urllib.parse import parse_qs, unquote, urlsplit
lazy from dataclasses import replace

from django import template
from django.template.base import token_kwargs
from django.template.context import RequestContext
from django.template.loader import render_to_string
from django.utils import dateformat, timezone
from django.utils.html import conditional_escape, format_html
from django.utils.safestring import mark_safe
lazy from django.conf import settings

from hq.platform.application.doors import door_words as _door_words, interface_label as _interface_label
from hq.platform.application.entity_links import (
    connection_anchor as anchor,
    declared_link as declared,
    entity_link,
    kind_label as label,
    web_url as _web_url,
)
from hq.platform.application.labels import DOT_MARKUP, human_bytes
from hq.platform.application.moments import (
    WHEN_FORMS as _WORDS_FORMS,
    ago as _ago,
    elapsed as _elapsed,
    span as said,
    when_day as _when_day,
    when_exact as _when_exact,
)
from hq.platform.application.money import money as _money
from hq.platform.application.page_parts import html_attributes
from hq.platform.application.printable import admitted
from hq.platform.application.timestamps import moment
from hq.platform.application.ui import MISSING, STATUS_VALUES, counted as _counted
from hq.platform.core.templatetags.line_breaks import breakable, code_spans, held, mark_text
lazy from hq.domains.control_plane.provider_spec import expiry_phrase
lazy from hq.platform.application import references, request_identity, security
lazy from hq.platform.application.derivations import this_year
lazy from hq.platform.application.problems import problems_about as _problems_about
lazy from hq.platform.application.references import reference_of
lazy from hq.platform.application.where_it_runs import named_links as _named_links
lazy from hq.platform.core.oidc import SIGNED_IN_SESSION_KEY

register = template.Library()
register.filter("breakable", breakable, is_safe=True)
register.filter("code_spans", code_spans, is_safe=True)

# The one form a link to a page outside HQ takes, whichever tag writes it.
OUTSIDE_LINK = "partials/_outside_link.html"


@register.filter
def counted(count, phrases):
    """``{{ n|counted:"project needs output,projects need output" }}``.

    One word may stand alone (``{{ n|counted:"change" }}``); a phrase gives both
    forms, comma separated, so its verb agrees too. See ``application.ui.counted``.
    The figure and its first word share a line (``line_breaks.held``).
    """
    return held(_counted(int(count or 0), *counted_phrases(phrases)))


@register.filter
def measured(value, unit):
    """``{{ miles|floatformat:2|measured:"mi" }}``: a figure and its unit, or a
    word the count leaves alone (``measured:"more"``), as one value, so the
    word never opens a line alone below its number. For a measure no other
    value tag says (``bytes``, ``seconds``, ``money``) and a word ``counted``
    would make plural; a missing figure is the missing mark, with no word."""

    if value is None or value == "":
        return or_empty(value)
    return held(f"{value} {unit}")


def counted_phrases(phrases) -> tuple[str, str | None]:
    """The filter argument as ``(one, many)``; ``many`` is None when it is left out."""
    one, _, many = str(phrases).partition(",")
    return one.strip(), many.strip() or None


@register.simple_tag
def empty_value():
    """The mark for a value that is not there, where there is no value to test."""
    return format_html('<span class="empty-value" title="None">{}</span>', MISSING)


@register.simple_tag
def dot():
    """The middle dot between two parts of one line of text, written straight
    after the first part. A no-break space holds it to that part and the line
    may break only after it, so no line starts with a dot or holds one alone;
    a reader hears the two parts. A run of whole parts that is a list of its
    own is a ``.dot-list``; Python joins text as ``labels.dotted`` does."""
    return mark_safe(DOT_MARKUP)  # noqa: S308 - HQ's own literal markup


@register.simple_tag
def arrow():
    """The arrow that ends a link onward, written straight after its last
    word, held to it by a no-break space and never read: the link's words
    say where it goes."""
    return mark_safe('&nbsp;<span aria-hidden="true">&rarr;</span>')


@register.filter(name="bytes")
def bytes_(value):
    """``{{ n|bytes }}``: a byte count as ``human_bytes`` says it, the format the
    controller's readings already use. Never ``filesizeformat``, which says the
    same count differently."""

    return held(human_bytes(value))


@register.filter
def money(value, form=""):
    """``{{ amount|money }}``: "$1,234.50", the one way a page writes an amount.

    ``{{ amount|money:"whole" }}`` leaves the cents off a figure that is
    scanned, never reconciled. A negative amount takes a true minus sign and a
    missing one the mark for a missing value. Never ``floatformat`` and
    ``intcomma`` behind a dollar sign, which say a negative amount as "$-5.00".
    """

    if form not in ("", "whole"):
        raise template.TemplateSyntaxError(f"money takes 'whole' or nothing; got {form!r}.")
    if value is None or value == "":
        return or_empty(value)
    return _money(value, cents=form != "whole")


@register.filter
def or_empty(value):
    """The value, or the one muted mark for a value that is not there.

    Built in, so every HQ and extension template has it without a load. Zero is
    a value and is shown; only None and the empty string are missing. A number
    passes as it is; anything else is what ``breakable`` makes of it.
    """
    if value is None or value == "":
        return format_html('<span class="empty-value" title="None">{}</span>', MISSING)
    if not isinstance(value, bool) and isinstance(value, Number):
        return value
    return breakable(value)


def _instant(value) -> date | datetime | None:
    """A datetime, a date, or either as an ISO stamp; None when it names neither."""

    if isinstance(value, date):  # a datetime is one too
        return value
    text = str(value or "").strip()
    found = moment(text) if "T" in text or " " in text else None
    if found is not None:
        return found
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _dated_mark(value: date):
    """A day as a mark beside a dated row: its month over its day, and its year
    under them only when it is not this one. A horizon can run years out (a
    passport, a license), and month-and-day alone makes a correctly sorted list
    read as a sorting bug; the current year on every row would be noise on the
    rows that are actually soon."""

    if isinstance(value, datetime):
        value = timezone.localtime(value) if timezone.is_aware(value) else value
    year = (
        "" if value.year == this_year() else format_html('<span class="timeline-year">{}</span>', breakable(value.year))
    )
    return format_html(
        '<span class="timeline-month">{}</span><span class="timeline-day">{}</span>{}',
        breakable(dateformat.format(value, "M")),
        breakable(value.day),
        year,
    )


# What ``when`` can be asked for: the words ``application.moments`` declares,
# and the one form that is markup, a dated row's mark.
_WHEN_FORMS = {**_WORDS_FORMS, "mark": _dated_mark}


@register.filter
def when(value, form=""):
    """``{{ moment|when }}``: the one way a page shows a moment, a date or an age.

    A ``<time>`` whose ``datetime`` is the instant as a machine reads it (what a
    table sorts on) and whose ``title`` is the exact moment, around the words
    ``application.ui`` writes in the form asked (``_WHEN_FORMS``, docs/Design.md).
    Takes a datetime, a date or an ISO stamp. Nothing shows the mark for a
    missing value; text that names no instant is shown as it came, its names held.
    """

    if form not in _WHEN_FORMS:
        raise template.TemplateSyntaxError(
            f"when takes {', '.join(repr(name) for name in _WHEN_FORMS if name)} or nothing; got {form!r}."
        )
    if value is None or value == "":
        return or_empty(value)
    found = _instant(value)
    if found is None:
        return breakable(value)
    return format_html(
        '<time datetime="{}" title="{}">{}</time>',
        found.isoformat(),
        _when_exact(found),
        _WHEN_FORMS[form](found),
    )


@register.filter
def ago(value):
    """``{{ moment|ago }}``: an age as bare words, for a sentence or an attribute.

    A datetime or an ISO stamp, in ``application.ui``'s phrasing. On its own
    in a cell or a readout, ``{{ moment|when:"ago" }}`` says the same words
    and keeps the exact moment behind them.
    """

    if isinstance(value, datetime):
        return _ago(value)
    return _elapsed(str(value or ""))


@register.filter
def day(value):
    """``{{ moment|day }}``: the day ``when:"day"`` names, as bare words, for
    a day written where only text may go."""

    return _when_day(value)


@register.filter
def signed_in(request):
    """``{{ request|signed_in }}``: how long ago this session's sign-in was, in hours or days.

    Never minutes: a menu that counts them is a clock nobody asked for. The
    session says when its sign-in was; one that does not (a password sign-in)
    falls back on the account's last login.
    """

    session = getattr(request, "session", None)
    when = moment(str(session.get(SIGNED_IN_SESSION_KEY) or "")) if session is not None else None
    when = when or getattr(request_identity.signed_in(request), "last_login", None)
    if when is None:
        return ""
    hours = int((timezone.now() - when).total_seconds() // 3600)
    if hours < 1:
        return "Signed in within the hour"
    if hours < 48:
        return held(f"Signed in {_counted(hours, 'hour', 'hours')} ago")
    return held(f"Signed in {_counted(hours // 24, 'day', 'days')} ago")


@register.filter
def span(days):
    """``{{ days_left|span }}``: a count of days as a length of time, from its one owner."""

    try:
        return said(int(days))
    except TypeError, ValueError:
        return MISSING


@register.filter
def seconds(value):
    """``{{ job.duration_seconds|seconds }}``: a run time as a person reads
    one: "45 s", "4 min 33 s", "1 h 2 min". Never a bare count of seconds."""

    try:
        whole = max(0, round(float(value)))
    except TypeError, ValueError:
        return or_empty(None)
    hours, rest = divmod(whole, 3600)
    minutes, left = divmod(rest, 60)
    if hours:
        return held(f"{hours} h {minutes} min")
    return held(f"{minutes} min {left} s" if minutes else f"{left} s")


@register.filter
def expiry(value):
    """``{{ not_after|expiry }}``: "22 Dec 2026 · 87 days", the one phrasing HQ uses for an end date."""

    return held(expiry_phrase(str(value or "")))


@register.filter
def ago_short(value):
    """``ago``, saying nothing rather than the missing mark when there is no moment."""

    # A datetime, or an ISO stamp as a provider wrote it, like ``ago``.
    found = value if isinstance(value, datetime) else moment(str(value or ""))
    return _ago(found) if found else ""


@register.filter
def readable(value):
    """A value that may be an ISO 8601 timestamp: ``when`` if it is, its names held if not."""

    if not isinstance(value, str) or "T" not in value:
        return breakable(value)
    return when(value)


@register.filter
def then(value, marks):
    """``marks`` split at ``|``: the first when ``value`` holds, the second
    (or none) when it does not. The mark a value takes depends on what the
    sentence says after it (``{% with mark=relay|then:"," %}``), and
    ``yesno`` cannot say a comma."""

    yes, _, no = str(marks).partition("|")
    return yes if value else no


def _request(context):
    """The request being drawn: the context's own, which an ``{% include ...
    only %}`` keeps, or else the variable a plain ``Context`` was given."""

    return context.request if isinstance(context, RequestContext) else context.get("request")


def _is_this_page(context, url: str) -> bool:
    """Whether ``url`` is the page being rendered, not a fragment of it."""

    request = _request(context)
    if request is None or not url or "#" in url:
        return False
    parts = urlsplit(url)
    return not parts.netloc and unquote(parts.path) == request.path


@register.simple_tag(takes_context=True)
def is_this_page(context, url):
    """``{% is_this_page url as here %}``: whether ``url`` is the view being
    drawn, its path and its query: a control that would only lead there is
    the view's own state, never a way to it."""

    url = str(url or "")
    return _is_this_page(context, url) and parse_qs(urlsplit(url).query) == parse_qs(
        _request(context).META.get("QUERY_STRING", "")
    )


@register.simple_tag(takes_context=True)
def entity(context, link, code=False, label="", mark=""):
    """One entity name, as the link builder answered for it.

    ``link`` is an ``application.entity_links.EntityLink``. Every mention of an
    entity on a page renders here, marked ``data-entity``. An external link
    opens in a new tab without a way back to this page. A link to the page it
    is on renders as plain text. ``mark`` is the punctuation after the name
    (or a list's ``forloop``), held to it as ``breakable`` holds it.
    """
    if link is None:
        return format_html('<span class="empty-value" title="None">{}</span>', MISSING) + conditional_escape(
            mark_text(mark)
        )
    # A count of nothing is still a name to show.
    if label is not None and label != "":
        link = replace(link, label=str(label))
    label = format_html("<code>{}</code>", breakable(link.label, mark)) if code else breakable(link.label, mark)
    if link.url and not link.external and _is_this_page(context, link.url):
        return format_html('<span data-entity="{}">{}</span>', link.kind_label, label)
    if link.url and link.external:
        return mark_safe(  # noqa: S308 - the template's own escaped output
            render_to_string(
                OUTSIDE_LINK,
                {"url": link.url, "arrow": True, "entity": link.kind_label, "content": label},
            ).strip()
        )
    if link.url:
        return format_html(
            '<a href="{}" data-entity="{}"{}>{}</a>',
            link.url,
            link.kind_label,
            format_html(' title="{}"', link.title) if link.title else "",
            label,
        )
    return format_html('<span data-entity="{}">{}</span>', link.kind_label, label)


@register.simple_tag
def cell_line(text, url=""):
    """One fact in a table cell written as a line (``.cell-line``), a link when
    ``url`` is given: its names held as ``breakable`` holds them. The line asks
    for its whole length around the box that cuts it (``.cut``, figures.css),
    and a cut one reads whole as any cut value does (tables.js, ``markCut``)."""

    drawn = breakable(text)
    # A name brings its own box and strut; words are cut by the line's box.
    inside = (
        drawn
        if drawn.startswith('<span class="token" data-sole')
        else format_html('<span class="cut">{}</span>', drawn)
    )
    if url:
        return format_html('<a class="cell-line" href="{}">{}</a>', url, inside)
    return format_html('<span class="cell-line">{}</span>', inside)


def _viewer(context):
    """Who the page is rendered for, as the application asks about them."""

    return security.web_principal(context["request"].user)


@register.simple_tag(takes_context=True)
def reference(context, row, field):
    """``{% reference expense "paid_from" as account %}``: the link for the
    reference a row holds, for ``{% entity %}``. None when it holds none, or
    one this viewer may not be told about."""

    return reference_of(row, field, principal=_viewer(context))


@register.inclusion_tag("partials/_related.html", takes_context=True)
def referenced_by(context, thing, identity="", cards=True):  # noqa: ARG001 - hq_sdk's contract, which extensions call with cards
    """Everything that names a thing, for that thing's page: a card per kind
    of reference, as every related list is drawn (``partials/_related.html``).

    ``{% referenced_by vehicle %}`` for a row of a model that can be referred
    to; ``{% referenced_by "machine" machine.name %}`` for a kind and what its
    page is addressed by. ``cards`` has one value left, the one extensions
    still pass. One statement, whatever names it.
    """

    principal = _viewer(context)
    if isinstance(thing, str):
        found = references.referenced_by(thing, str(identity), principal=principal)
    else:
        found = references.referenced_by_row(thing, principal=principal)
    return {"groups": found, "request": context.get("request")}


@register.filter
def interface_label(value):
    """An interface, or a list of them joined, as the audit log names it: ``mcp`` reads "MCP"."""

    if isinstance(value, str):
        return _interface_label(value)
    return ", ".join(_interface_label(interface) for interface in value or ())


@register.filter
def door_words(interface):
    """An interface in a sentence, beside who came through it: ``api`` reads "over the API"."""

    return _door_words(str(interface or ""))


@register.filter
def kind_label(kind):
    """What a registry kind is called in a sentence: ``tailscale.device`` reads "Tailnet device"."""

    return label(str(kind or ""))


@register.filter
def entity_of(kind, identity):
    """``{% entity "machine"|entity_of:name %}``: the link builder's answer for one thing."""

    return entity_link(str(kind), str(identity or ""))


@register.filter
def declared_link(link):
    """A link an emitter declared (a label and a url), as an entity mention."""

    return declared(link.label, link.url)


@register.filter
def outside(url):
    """``{% entity url|outside label=name %}``: a page outside HQ, named.

    A url that is not a web address leaves the name unlinked, so a page builds
    no href from data HQ did not write (``web_url``)."""

    return declared("", _web_url(url), kind="dependency")


@register.filter
def connection_anchor(ref):
    """The id of a connection's row, the target of its entity link."""

    return anchor(str(ref or ""))


# An identifier: a key, a hostname, an address, a path, a port spec. Anything
# with a character plain prose does not use.
_IDENTIFIER = re.compile(r"[-_./:@=#]")


@register.filter
def readout(value):
    """A value as a readout shows it: an identifier in code, anything a person
    would write in a sentence (a count, a word, a phrase) as text.

    Every value in code made "11", "On" and "Need approval" look like keys to
    copy, and put a gray chip around each number in a list of three numbers.
    """

    text = str(admitted(value))
    if " " not in text.strip() and _IDENTIFIER.search(text):
        return format_html("<code>{}</code>", breakable(text))
    # A phrase can still name something ("restart example-web"): its names
    # are held as tokens, its words stay text.
    return breakable(text)


@register.filter
def host(url):
    """The host a web address names: what an outside link says it opens."""

    return urlsplit(str(url or "")).hostname or ""


@register.filter
def web_url(value):
    """An href from data someone else wrote: http(s) or nothing."""

    return _web_url(value)


@register.filter
def named_links(text):
    """Free text as plain stretches and the links of the names HQ knows in
    it, for ``partials/_linked_text.html``."""
    return _named_links(text)


@register.simple_tag(takes_context=True)
def problems_about(context, url=""):
    """The open problems about the thing at ``url``, by default the page this
    is drawn on, for ``partials/_problems.html``. One read of a stored answer,
    made only by a page that asks."""
    request = context.get("request")
    if request is None:
        return ()
    return _problems_about(url or request.path, _viewer(context))


@register.filter
def urgent(problems):
    """The urgent ones of ``problems``: the only ones framed above a page."""
    return [item for item in problems or () if item.get("status") == "serious"]


@register.filter
def not_urgent(problems):
    """The rest of ``problems``, each drawn as a line."""
    return [item for item in problems or () if item.get("status") != "serious"]


@register.simple_tag
def rendered_copy() -> datetime | None:
    """When this saved copy of HQ was saved (``SEVERINO_RENDERED_COPY``), which
    is the moment it was rendered; None for a running HQ."""

    return timezone.now() if settings.SEVERINO_RENDERED_COPY else None


class SlotsNode(template.Node):
    """A block tag drawn by its partial, each part it encloses handed over as
    the variable it is named for (``slotted``)."""

    def __init__(self, takes: Takes, parts: dict[str, template.NodeList], options: dict) -> None:
        self.takes = takes
        self.options = options
        self.child_nodelists = tuple(f"nodelist_{name}" for name in parts)
        for name, nodelist in parts.items():
            setattr(self, f"nodelist_{name}", nodelist)

    def render(self, context) -> str:
        values = self.takes.values({name: value.resolve(context) for name, value in self.options.items()})
        for attribute in self.child_nodelists:
            # Rendered by the page's own template, so escaped already. A part
            # left empty leaves an option of its name as it is.
            part = mark_safe(getattr(self, attribute).render(context).strip())  # noqa: S308 - the template's own escaped output
            name = attribute.removeprefix("nodelist_")
            if part or name not in values:
                values[name] = part
        return context.template.engine.get_template(self.takes.partial).render(context.new(values)).strip()


class Takes:
    """What a slotted tag takes: its parts, its options, whether a page may
    give it ``data_*`` options, and how the options become the partial's
    variables. Its signature and the parts after its first are what the
    template contract records for the tag."""

    # The option that, given, makes the tag stand alone, enclosing nothing.
    bare_with = ""

    def __init__(
        self, name: str, partial: str, parts: tuple[str, ...], options: frozenset[str], shape: Shape | None
    ) -> None:
        self.name = name
        self.partial = partial
        self.parts = parts
        self.options = options
        self.shape = shape
        keyword = inspect.Parameter.KEYWORD_ONLY
        self.__signature__ = inspect.Signature(
            [
                inspect.Parameter("content", inspect.Parameter.POSITIONAL_OR_KEYWORD),
                *(inspect.Parameter(option, keyword, default="") for option in sorted(options)),
                *([inspect.Parameter("data", inspect.Parameter.VAR_KEYWORD)] if shape else []),
            ]
        )

    def __call__(self, *args, **kwargs) -> None:
        raise TypeError(f"{self.name} is a template tag, drawn by {self.partial}")

    def admits(self, names: set[str]) -> bool:
        return all(name in self.options or (self.shape and name.startswith("data_")) for name in names)

    def values(self, given: dict) -> dict:
        return self.shape(self.name, given) if self.shape else given


type Shape = Callable[[str, dict], dict]


def slotted(
    name: str,
    partial: str,
    parts: tuple[str, ...],
    options: frozenset[str],
    *,
    shape: Shape | None = None,
    library: template.Library = register,
) -> None:
    """Register ``{% name option=value %}first{% second %}...{% endname %}``:
    markup a page would otherwise write by hand, drawn by ``partial`` with
    each part as the variable it is named for. The first part follows the
    opening tag; the others may be left out, and come in ``parts``' order.
    A tag with a ``shape`` also takes ``data_*`` options, and its shape turns
    what a page gave into the partial's variables. ``library`` is the one
    it is registered in."""

    end = f"end{name}"
    takes = Takes(name, partial, parts, options, shape)

    def compile_(parser, token) -> SlotsNode:
        bits = token.split_contents()
        given = token_kwargs(bits[1:], parser)
        if len(given) != len(bits) - 1 or not takes.admits(set(given)):
            rest = ", data_*" if shape else ""
            raise template.TemplateSyntaxError(f"{name} takes {', '.join(sorted(options))}{rest} as name=value")
        found = {part: template.NodeList() for part in parts}
        ahead = list(parts)
        while True:
            found[ahead.pop(0)] = parser.parse((*ahead, end))
            marker = parser.next_token().contents
            if marker == end:
                return SlotsNode(takes, found, given)
            ahead = ahead[ahead.index(marker) :]

    # The template contract reads a tag's parameters from what it wraps.
    compile_.__wrapped__ = takes  # type: ignore[attr-defined]  # a function's attribute, which typeshed does not declare
    library.tag(name, compile_)


CARD_ELEMENTS = frozenset(("section", "article", "figure", "details", "div"))


def card_frame(name: str, given: dict) -> dict:
    """A card's opening and closing tags. Built here rather than in its
    partial, since the element is a choice and every attribute but the class
    is drawn only when it has a value, as ``html_attributes`` draws one."""

    element = given.get("element", "section")
    if element not in CARD_ELEMENTS:
        raise template.TemplateSyntaxError(f"{name} element= is one of {', '.join(sorted(CARD_ELEMENTS))}")
    named = {
        "class": " ".join(("card", given.get("css") or "")).strip(),
        "id": given.get("id"),
        "aria-labelledby": given.get("heading"),
        "aria-label": given.get("label"),
        "aria-busy": "true" if given.get("busy") else None,
        "open": True if given.get("open") else None,
        "data-alone": True if given.get("alone") else None,
    }
    named |= {option.replace("_", "-"): value for option, value in given.items() if option.startswith("data_")}
    # The element is one of CARD_ELEMENTS, a tag name and never text, and
    # html_attributes escapes the attributes.
    return {
        "opening": mark_safe(f"<{element}{html_attributes(named)}>"),  # noqa: S308 - see above
        "closing": mark_safe(f"</{element}>"),  # noqa: S308 - see above
    }


slotted("section_head", "partials/_section_head.html", ("heading", "aside"), frozenset(("id", "lede")))
slotted(
    "outside_link",
    OUTSIDE_LINK,
    ("content",),
    frozenset(("url", "css", "tone", "tip", "title", "label", "modal", "entity")),
)
# A list of short steps or checks, read down its columns, or numbered when
# they are done in order, and one step of it: its icon, toned by how it
# stands, or its number, then what it says.
slotted("steps", "partials/_steps.html", ("content",), frozenset(("numbered",)))
slotted("step", "partials/_step.html", ("content",), frozenset(("icon", "tone", "done", "title")))
# A row of links, each a chip of its own.
slotted("link_chips", "partials/_link_chips.html", ("content",), frozenset())
slotted(
    "card",
    "partials/_card.html",
    ("content",),
    frozenset(("element", "id", "css", "heading", "label", "busy", "open", "alone")),
    shape=card_frame,
)


# How a reading's note stands: said plainly, a problem, or past its time.
NOTE_TONES = frozenset(("", "attention", "stale"))


def reading_frame(name: str, given: dict) -> dict:
    """A reading's options as its partial draws them: its tone as the shared
    tone class, and each ``data_*`` option as an attribute of the reading."""

    tone, note_tone = given.get("tone") or "", given.get("note_tone") or ""
    if tone and tone not in STATUS_VALUES:
        raise template.TemplateSyntaxError(f"{name} tone= is one of {', '.join(sorted(STATUS_VALUES))}")
    if note_tone not in NOTE_TONES:
        raise template.TemplateSyntaxError(f"{name} note_tone= is one of {', '.join(sorted(filter(None, NOTE_TONES)))}")
    data = {option.replace("_", "-"): value for option, value in given.items() if option.startswith("data_")}
    return {
        **{option: value for option, value in given.items() if not option.startswith("data_")},
        "tone_class": f" tone-{tone}" if tone not in ("", "neutral") else "",
        "attributes": html_attributes(data),
    }


# A row of readings and one reading of it: its mark, its figure, its quiet
# lines and a note on how it stands, leading to a page or opening a panel.
slotted("readings", "partials/_readings.html", ("content",), frozenset())
slotted(
    "reading",
    "partials/_reading.html",
    ("lead", "lines", "note", "heading", "panel", "foot"),
    frozenset(("icon", "url", "modal", "id", "title", "unit", "tone", "note_tone")),
    shape=reading_frame,
)
