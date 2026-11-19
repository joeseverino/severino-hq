import re
from collections.abc import Callable
from datetime import date, datetime
from numbers import Number
from typing import Annotated, Any
from urllib.parse import parse_qs, unquote, urlsplit
lazy from dataclasses import replace

from django import template
from django.template.base import token_kwargs
from django.template.context import RequestContext
from django.template.loader import render_to_string
from django.utils import dateformat, timezone
from django.utils.html import conditional_escape, format_html
from django.utils.safestring import SafeData, mark_safe
lazy from django.conf import settings

from hq.platform.application.describe import Describe
from hq.platform.application.doors import interface_label as _interface_label
from hq.platform.application.entity_links import (
    connection_anchor as anchor,
    declared_link as declared,
    entity_link,
    kind_label as label,
    record_link,
    web_url as _web_url,
)
from hq.platform.application.labels import DOT_MARKUP, human_bytes
from hq.platform.application.moments import (
    WHEN_FORMS as _WORDS_FORMS,
    moment,
    span as said,
    stamp_day,
    when_exact as _when_exact,
)
from hq.platform.application.money import money as _money
from hq.platform.application.printable import admitted
from hq.platform.application.ui import MISSING, counted as _counted, measured as _measured, no_value
from hq.platform.core.template_parts import Option, Part, SlotsNode, slotted
from hq.platform.core.templatetags.facts_tags import FACT
from hq.platform.core.templatetags.line_breaks import breakable, button_words, code_spans, held, mark_text
lazy from hq.domains.control_plane.provider_spec import expiry_phrase
lazy from hq.platform.application import references, request_identity, security
lazy from hq.platform.application.derivations import this_year
lazy from hq.platform.application.freshness import LAST_READ
lazy from hq.platform.application.problems import problems_about as _problems_about
lazy from hq.platform.application.references import reference_of
lazy from hq.platform.application.where_it_runs import named_links as _named_links
lazy from hq.platform.core.oidc import SIGNED_IN_SESSION_KEY

register = template.Library()
register.filter("breakable", breakable, is_safe=True)
register.filter("button_words", button_words, is_safe=True)
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
    """``{{ miles|measured:"mi,2" }}``: a figure and its unit, or a word the
    count leaves alone (``measured:"more"``), as one value, so the word never
    opens a line alone below its number. A count of decimals after a comma
    rounds the figure as ``floatformat`` would (``"%,-1"``, only when not
    whole). For a measure no other value tag says (``bytes``, ``seconds``,
    ``money``) and a word ``counted`` would make plural. ``ui.measured``
    writes it; a missing figure is the missing mark, with no word."""

    if no_value(value):
        return empty_value()
    return held(_measured(value, *measured_unit(unit)))


def measured_unit(argument) -> tuple[str, int | None]:
    """The filter argument as ``(unit, places)``; ``places`` is None when it is left out."""

    unit, comma, places = str(argument).rpartition(",")
    if not comma:
        return places, None
    try:
        return unit, int(places)
    except ValueError:
        raise template.TemplateSyntaxError(
            f"measured takes a unit and a count of decimals; got {argument!r}."
        ) from None


def counted_phrases(phrases) -> tuple[str, str | None]:
    """The filter argument as ``(one, many)``; ``many`` is None when it is left out."""
    one, _, many = str(phrases).partition(",")
    return one.strip(), many.strip() or None


def empty_value():
    """The mark for a value that is not there, where there is no value to test:
    its one markup, which ``or_empty`` and ``entity`` draw too."""
    return format_html('<span class="empty-value" title="None">{}</span>', MISSING)


def dot():
    """The middle dot between two parts of one line of text, written straight
    after the first part. A no-break space holds it to that part and the line
    may break only after it, so no line starts with a dot or holds one alone;
    a reader hears the two parts. A run of whole parts that is a list of its
    own is ``{% list kind="dots" %}``; Python joins text as ``labels.dotted`` does."""
    return mark_safe(DOT_MARKUP)  # noqa: S308 - HQ's own literal markup


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
    if no_value(value):
        return empty_value()
    return _money(value, cents=form != "whole")


@register.filter
def or_empty(value):
    """The value, or the one muted mark for a value that is not there.

    Built in, so every HQ and extension template has it without a load. What
    is missing is ``ui.no_value``'s: zero is a value and is shown. A number
    passes as it is; anything else is what ``breakable`` makes of it. Markup
    around the value is ``{% present %}``'s.
    """
    if no_value(value):
        return empty_value()
    if not isinstance(value, bool) and isinstance(value, Number):
        return value
    return breakable(value)


def present(content: Callable[[], str], value: Annotated[Any, Describe("The value whose markup it holds.")]) -> str:
    """``{% present repo.release %}{% entity repo.release.link %}{% endpresent %}``:
    what the tag encloses, drawn only when ``value`` is there, and otherwise
    the mark ``or_empty`` draws, for a value shown in markup (a ``<code>``, a
    link, a status, an include) that the filter cannot hold."""

    return empty_value() if no_value(value) else content()


class PresentNode(template.Node):
    child_nodelists = ("nodelist",)

    def __init__(self, value, nodelist: template.NodeList) -> None:
        self.value = value
        self.nodelist = nodelist

    def render(self, context) -> str:
        return present(lambda: self.nodelist.render(context), self.value.resolve(context))


@register.tag("present")
def present_tag(parser, token) -> PresentNode:
    bits = token.split_contents()
    if len(bits) != 2:
        raise template.TemplateSyntaxError("present takes one value: {% present value %}...{% endpresent %}")
    nodelist = parser.parse(("endpresent",))
    parser.delete_first_token()
    return PresentNode(parser.compile_filter(bits[1]), nodelist)


def _instant(value) -> date | datetime | None:
    """A datetime, a date, or either as an ISO stamp; None when it names neither."""

    if isinstance(value, date):  # a datetime is one too
        return value
    text = str(value or "").strip()
    found = moment(text) if "T" in text or " " in text else None
    return found if found is not None else stamp_day(text)


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
        '<span class="eyebrow">{}</span><span class="timeline-day">{}</span>{}',
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
    if no_value(value):
        return empty_value()
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
def last_read(value):
    """``{{ moment|last_read }}``: when a reading was taken, said as every page
    says it (``freshness.LAST_READ``), its age a ``when`` that shows the moment."""

    return format_html("{} {}", LAST_READ, when(value, "ago"))


@register.filter
def when_text(value, form=""):
    """``{{ moment|when_text:"ago" }}``: the words ``when`` writes in a form,
    as plain text, where a ``<time>`` cannot stand: an attribute, or a line
    in a table cell, which sorts on the first ``<time>`` it holds. Nothing for
    a missing moment or text that names none."""

    if form not in _WORDS_FORMS:
        raise template.TemplateSyntaxError(
            f"when_text takes {', '.join(repr(name) for name in _WORDS_FORMS if name)} or nothing; got {form!r}."
        )
    found = _instant(value)
    return _WORDS_FORMS[form](found) if found is not None else ""


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
    one: "45 s", "4 min 33 s", "1 h 2 min", "30 min". Never a bare count of
    seconds, and never a part that is zero after a larger one."""

    try:
        whole = max(0, round(float(value)))
    except TypeError, ValueError:
        return or_empty(None)
    hours, rest = divmod(whole, 3600)
    minutes, left = divmod(rest, 60)
    larger, unit, smaller, small_unit = (hours, "h", minutes, "min") if hours else (minutes, "min", left, "s")
    if not larger:
        return held(f"{left} s")
    return held(f"{larger} {unit} {smaller} {small_unit}" if smaller else f"{larger} {unit}")


@register.filter
def expiry(value):
    """``{{ not_after|expiry }}``: "22 Dec 2026 · 87 days", the one phrasing HQ uses for an end date."""

    return held(expiry_phrase(str(value or "")))


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


def entity(
    context,
    link: Annotated[Any, Describe("The thing's link, as the link builder answered for it.")],
    code: Annotated[bool, Describe("Its name drawn as code.")] = False,
    label: Annotated[Any, Describe("The words it is named by, in place of its own.")] = "",
    mark: Annotated[Any, Describe("The punctuation after the name, held to it, or a list's `forloop`.")] = "",
):
    """One entity name, as the link builder answered for it.

    ``link`` is an ``application.entity_links.EntityLink``. Every mention of an
    entity on a page renders here, marked ``data-entity``. An external link
    opens in a new tab without a way back to this page. A link to the page it
    is on renders as plain text. ``mark`` is the punctuation after the name
    (or a list's ``forloop``), held to it as ``breakable`` holds it.
    """
    if link is None:
        return empty_value() + conditional_escape(mark_text(mark))
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


def cell_line(text, url="", *, lines=2):
    """One fact in a table cell written as a line (``.cell-line``), a link when
    ``url`` is given: its names held as ``breakable`` holds them. The line asks
    for its whole length around the box that cuts it (``.cut``, figures.css),
    and a cut one reads whole as any cut value does (tables.js, ``markCut``).
    It takes two lines at most, or one (``lines=1``) for a fact that keeps its
    row as tall as its neighbors. ``{% cell_line text=... %}`` draws it."""

    drawn = breakable(text)
    # A name brings its own box; words are cut by the line's box.
    inside = (
        drawn
        if drawn.startswith('<span class="token" data-sole')
        else format_html('<span class="cut">{}</span>', drawn)
    )
    if lines not in (1, 2):
        raise ValueError(f"A cell's line takes 1 or 2 lines, never {lines!r}.")
    one = format_html(' data-lines="{}"', lines) if lines == 1 else ""
    if url:
        return format_html('<a class="cell-line"{} href="{}">{}</a>', one, url, inside)
    return format_html('<span class="cell-line"{}>{}</span>', one, inside)


def _cell_line_frame(_name: str, given: dict) -> dict:
    """The partial's variables: the first line drawn from ``text`` when the
    tag stands alone (the markup it encloses otherwise), and the second line's
    words held as ``breakable`` holds them unless they are markup already."""

    text, detail = given["text"], given["detail"]
    line = cell_line(text, given["url"], lines=given["lines"]) if text not in ("", None) else ""
    below = detail if isinstance(detail, SafeData) else breakable(detail) if detail not in ("", None) else ""
    return {"line": line, "lines": given["lines"], "detail": below}


CELL_LINE = Part(
    name="cell_line",
    classes=("cell-line", "cell-detail"),
    partial="partials/_cell_line.html",
    parts=("content", "detail"),
    about=(
        "A fact in a table cell as a line of its own, and the words said quietly on the line under it: "
        "given `text=` it encloses nothing, its words a link to `url=`; a line of markup is its block, "
        "its second line after `{% detail %}`. A second line that is a status, code or a link is a "
        "`{% list %}` item."
    ),
    options={
        "text": Option(about="Its words, which make it one tag with no end."),
        "url": Option(about="The page its words link to."),
        "detail": Option(about="The words of the line under it, said quietly."),
        "lines": Option(
            default=2, choices=(1, 2), about="The lines its first line may take: one keeps its row as tall as the rest."
        ),
    },
    bare_with="text",
    shape=_cell_line_frame,
    public=True,
    example='{% cell_line %}{% entity "machine"|entity_of:"example-host-01" %}{% detail %}Docker host{% endcell_line %}',
)


@register.tag(name="cell_line")
def compile_cell_line(parser, token) -> SlotsNode:
    bits = token.split_contents()[1:]
    given = token_kwargs(list(bits), parser)
    if len(given) != len(bits) or not CELL_LINE.admits(set(given)):
        raise template.TemplateSyntaxError(CELL_LINE.refused())
    if CELL_LINE.bare_with in given:
        return SlotsNode(CELL_LINE, {}, given)
    if "url" in given:
        raise template.TemplateSyntaxError("cell_line url= links its text=; a line of markup holds its own link")
    content = parser.parse(("detail", "endcell_line"))
    parts = {"content": content, "detail": template.NodeList()}
    if parser.next_token().contents == "detail":
        parts["detail"] = parser.parse(("endcell_line",))
        parser.delete_first_token()
    return SlotsNode(CELL_LINE, parts, given)


# The template contract reads a tag's parameters from what it wraps.
compile_cell_line.__wrapped__ = CELL_LINE  # type: ignore[attr-defined]  # a function's attribute, which typeshed does not declare


def _viewer(context):
    """Who the page is rendered for, as the application asks about them."""

    return security.web_principal(context["request"].user)


def reference(
    context,
    row: Annotated[Any, Describe("The record that holds the reference.")],
    field: Annotated[str, Describe("The field it is held in.")],
):
    """``{% reference expense "paid_from" as account %}``: the link for the
    reference a row holds, for ``{% entity %}``. None when it holds none, or
    one this viewer may not be told about."""

    return reference_of(row, field, principal=_viewer(context))


def referenced_by(
    context,
    thing: Annotated[Any, Describe("A record, or the kind of thing it is.")],
    identity: Annotated[Any, Describe("Given a kind, what its page is addressed by.")] = "",
):
    """Everything that names a thing, for that thing's page: a card per kind
    of reference, as every related list is drawn (``partials/_related.html``).

    ``{% referenced_by vehicle %}`` for a row of a model that can be referred
    to; ``{% referenced_by "machine" machine.name %}`` for a kind and what its
    page is addressed by. One statement, whatever names it.
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
def record(row, section=""):
    """``{% entity row|record label=row.name %}``: a record named by its own
    page, which its model declares (``get_absolute_url``), never a route and
    key a template spells; ``row|record:"updates"`` opens it at ``#updates``.
    No record names nothing, drawn as the missing mark."""

    return None if row is None else record_link(row, str(section or ""))


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


slotted(
    library=register,
    part=Part(
        name="outside_link",
        classes=("external-link",),
        draws=(FACT,),
        partial=OUTSIDE_LINK,
        about=(
            "A link to a page outside HQ around markup of the page's own (a chip, an icon, a card); "
            "a name is `{% entity url|outside label=name %}`."
        ),
        options={
            "url": Option(about="Where it leads; anything but a web address draws what it holds, unlinked."),
            "css": Option(about="The classes of where it stands."),
            "tone": Option(about="A status tone its words are drawn in."),
            "tip": Option(about="What a press or a hover shows, in place of the page it opens."),
            "title": Option(about="Its `title`, which otherwise names the page it opens."),
            "label": Option(about="The name a reader hears, for a link whose markup says none."),
            "modal": Option(about="The dialog it opens where script runs, the page being its fallback."),
            "entity": Option(about="The kind of thing it names, as `data-entity`."),
        },
        public=True,
        example='{% outside_link url="https://example.com/docs/" %}<span class="tag">Docs</span>{% endoutside_link %}',
    ),
)
slotted(
    library=register,
    part=Part(
        name="empty_value",
        about="The mark for a value that is not there, where there is no value to test; `|or_empty` tests one.",
        function=empty_value,
        public=True,
        example="{% empty_value %}",
    ),
)
slotted(
    library=register,
    part=Part(
        name="dot",
        about="The middle dot between two parts of one line, written straight after the first.",
        function=dot,
        public=True,
        example="{{ count }}{% dot %}{{ age }}",
    ),
)
slotted(
    library=register,
    part=Part(
        name="arrow",
        about="The arrow that ends a link onward, written straight after its last word.",
        function=arrow,
        public=True,
        example='<a href="/example/">All of them{% arrow %}</a>',
    ),
)
slotted(
    library=register,
    part=Part(
        name="entity",
        about="One thing named, as the link builder answered for it: a link to its page, or its name.",
        function=entity,
        public=True,
        example='{% entity "machine"|entity_of:name %}',
    ),
)
slotted(
    library=register,
    part=Part(
        name="reference",
        about="The link for the reference a record holds, for `{% entity %}`, or none the viewer may not see.",
        function=reference,
        public=True,
        example='{% reference expense "paid_from" as account %}{% entity account %}',
    ),
)
slotted(
    library=register,
    part=Part(
        name="referenced_by",
        about="Everything that names a thing, for that thing's page, a card per kind of reference.",
        partial="partials/_related.html",
        function=referenced_by,
        public=True,
        example='{% referenced_by "machine" machine.name %}',
    ),
)
# The template contract reads a tag's declaration from what it wraps.
present_tag.__wrapped__ = Part(  # type: ignore[attr-defined]  # a function's attribute, which typeshed does not declare
    name="present",
    about="What it encloses, drawn only when the value is there, else the mark for a missing value.",
    function=present,
    public=True,
    example="{% present repo.release %}<code>{{ repo.release }}</code>{% endpresent %}",
)
