import re
from datetime import date, datetime
from html import unescape
from numbers import Number
from urllib.parse import parse_qs, unquote, urlsplit
lazy from dataclasses import replace

from django import template
from django.template.base import token_kwargs
from django.template.loader import render_to_string
from django.utils import dateformat, timezone
from django.utils.html import conditional_escape, format_html
from django.utils.safestring import mark_safe
lazy from django.conf import settings

from hq.platform.application.entity_links import (
    connection_anchor as anchor,
    declared_link as declared,
    entity_link,
    kind_label as label,
    web_url as _web_url,
)
from hq.platform.application.labels import human_bytes
from hq.platform.application.moments import (
    ago as _ago,
    elapsed as _elapsed,
    span as said,
    when as _when,
    when_day as _when_day,
    when_exact as _when_exact,
)
from hq.platform.application.money import money as _money
from hq.platform.application.printable import admitted
from hq.platform.application.timestamps import moment
from hq.platform.application.ui import MISSING, counted as _counted
lazy from hq.domains.control_plane.provider_spec import expiry_phrase
lazy from hq.platform.application import references, request_identity, security
lazy from hq.platform.application.derivations import this_year
lazy from hq.platform.application.problems import problems_about as _problems_about
lazy from hq.platform.application.references import reference_of
lazy from hq.platform.application.where_it_runs import named_links as _named_links, where_it_runs as _where_it_runs
lazy from hq.platform.core.oidc import SIGNED_IN_SESSION_KEY

register = template.Library()

# The one form a link to a page outside HQ takes, whichever tag writes it.
OUTSIDE_LINK = "partials/_outside_link.html"


@register.filter
def counted(count, phrases):
    """``{{ n|counted:"project needs output,projects need output" }}``.

    One word may stand alone (``{{ n|counted:"change" }}``); a phrase gives both
    forms, comma separated, so its verb agrees too. See ``application.ui.counted``.
    """
    return _counted(int(count or 0), *counted_phrases(phrases))


def counted_phrases(phrases) -> tuple[str, str | None]:
    """The filter argument as ``(one, many)``; ``many`` is None when it is left out."""
    one, _, many = str(phrases).partition(",")
    return one.strip(), many.strip() or None


@register.simple_tag
def empty_value():
    """The mark for a value that is not there, where there is no value to test."""
    return format_html('<span class="empty-value" title="None">{}</span>', MISSING)


@register.filter(name="bytes")
def bytes_(value):
    """``{{ n|bytes }}``: a byte count as ``human_bytes`` says it, the format the
    controller's readings already use. Never ``filesizeformat``, which says the
    same count differently."""

    return human_bytes(value)


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
    if isinstance(value, Number) and not isinstance(value, bool):
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


# What ``when`` can be asked for, and the words each one shows.
_WHEN_FORMS = {
    "": _when,
    "ago": lambda value: _ago(value) if isinstance(value, datetime) else _when_day(value),
    "day": _when_day,
    "exact": _when_exact,
    "mark": _dated_mark,
}


@register.filter
def when(value, form=""):
    """``{{ moment|when }}``: the one way a page shows a moment, a date or an age.

    A ``<time>`` whose ``datetime`` is the instant as a machine reads it (which
    is what a table sorts on) and whose ``title`` is the exact moment, around
    the words ``application.ui`` writes:

    - ``{{ at|when }}``: "Oct 3, 9:29 AM"; a date reads "Oct 3".
    - ``{{ at|when:"ago" }}``: "5 days ago", the moment itself on hover.
    - ``{{ at|when:"day" }}``: the day a moment fell on.
    - ``{{ at|when:"exact" }}``: "Oct 3, 2026, 9:29:15 AM CDT".
    - ``{{ at|when:"mark" }}``: the day as a dated mark, month over day.

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
        return f"Signed in {_counted(hours, 'hour', 'hours')} ago"
    return f"Signed in {_counted(hours // 24, 'day', 'days')} ago"


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
        return f"{hours} h {minutes} min"
    return f"{minutes} min {left} s" if minutes else f"{left} s"


@register.filter
def expiry(value):
    """``{{ not_after|expiry }}``: "22 Dec 2026 · 87 days", the one phrasing HQ uses for an end date."""

    return expiry_phrase(str(value or ""))


@register.filter
def posture_state(posture, check_id):
    """``{{ posture|posture_state:check.id }}``: met, unmet, or unavailable."""

    return posture.state_of(check_id)


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


# Where an address, a hostname, a path or an image reference may wrap: after a
# slash, after a colon run (never inside a scheme's "://" or an IPv6 "::"), and
# after a dot unless digits stand either side of it, so a version ("2.39.8")
# and an IPv4 address are each one piece. An address's query wraps after its
# "?" and each "&" (escaped by then), so a word of it stays whole.
_BREAK_AFTER = re.compile(r"(/|:+|\?|&amp;|(?<!\d)\.|\.(?!\d))(?=[^/:])")
# A seam as markup: an empty span whose stylesheet text is a zero-width space
# (base.css). A `<wbr>` would do in WebKit, but Chromium breaks at one even
# where `white-space: nowrap` holds its line, and a zero-width space in the
# text itself would be copied with the name.
_SEAM = '<span class="seam"></span>'


# A part with a hyphen inside it ("nginx-proxy-manager") is kept on one line:
# a browser breaks after a hyphen, and a name split there reads as two. A flag
# ("-a") has nothing before its hyphen to break from.
_INNER_HYPHEN = re.compile(r"[^-\s]-")
# A seam inside a word, between two of its letters or digits: an address, a
# path, a host name. A dot after a bracket ("(.zip)") is punctuation.
_INNER_SEAM = re.compile(r"\w(?:/|:+|\.)\w")
_WORDS = re.compile(r"(\s+)")
# A figure in a phrase: an amount, a time, an address of digits ("$12.00",
# "9:29", "172.18.0.2"). Nothing in one is a place a line breaks.
_FIGURE = re.compile(r"\W*\d+(?:[.:,]\d+)*\W*")
# Where a browser may break a hyphenated part: after each inner hyphen.
_AFTER_HYPHEN = re.compile(r"(?<=[^-\s]-)")
# A piece of a name shorter than this, alone on a line ("1" under
# "portainer-agent-"), reads as a stray mark rather than part of the name.
_SCRAP = 3


def _glued(pieces: list[str]) -> list[list[str]]:
    """``pieces`` grouped so that no group is a scrap: a short piece goes with
    the one before it, and a short first piece with the one after."""

    groups: list[list[str]] = []
    for piece in pieces:
        if groups and (len(unescape(piece)) < _SCRAP or len(unescape("".join(groups[-1]))) < _SCRAP):
            groups[-1].append(piece)
        else:
            groups.append([piece])
    return groups


# A label of a name, with the seam after it: letters, digits, hyphens. A
# bracket or a quote ("com']") makes the run a value that holds names, not one.
_LABEL = re.compile(r"[\w-]+(?:\.|/|:+)?")


def _labels_held(pieces: list[str]) -> list[str]:
    """``pieces`` with each dotted name's end labels held to their neighbors:
    the last label stays with the one before it ("example.com"), and the
    first with the next while the name still has a seam left to break at. A
    label alone on a line ("com") reads as a word rather than part of a name.
    A hyphenated first label already reads as a name, so it keeps its seam:
    held to the next label, the pair could break only at a hyphen.
    Applied to a sole token, a name that is its value whole."""

    out: list[str] = []
    start = 0
    while start < len(pieces):
        end = start
        while end < len(pieces) - 1 and pieces[end].endswith("."):
            end += 1
        run = pieces[start : end + 1]
        if all(_LABEL.fullmatch(piece) for piece in run) and len(run) > 1:
            run = [*run[:-2], run[-2] + run[-1]]
        if len(run) > 2 and not _INNER_HYPHEN.search(run[0]):
            run = [run[0] + run[1], *run[2:]]
        out.extend(run)
        start = end + 1
    return out


def _joined(text: str) -> str:
    return f'<span class="joined">{text}</span>'


def _held(group: list[str], first: bool, last: bool) -> str:
    """A group at an end of its part, held together. The hold's edges sit
    before a hyphen, where no line may break, never after one: a browser
    decides a break at an element's edge by the box around both sides, and
    where the part is text with no box of its own (a name cut in a cell),
    that box would let the line break there."""

    text = "".join(group)
    if len(group) == 1:
        return text
    if first and last:
        return _joined(text)
    if first:
        return _joined(text[:-1]) + "-"
    return group[0][:-1] + _joined("-" + "".join(group[1:]))


def _whole(part: str, boxed: bool = False) -> str:
    """A part, kept on one line when a hyphen is inside it. Where it must break
    after a hyphen all the same, a scrap at either end stays with its
    neighbor; one inside it has words on both sides to share its line.

    ``boxed`` puts its text in a box of its own (``.part``), which a table
    cell cuts at the cell's edge while the part around it asks for its whole
    length and holds out for none of it (figures.css)."""

    if not _INNER_HYPHEN.search(part):
        return part
    groups = _glued(_AFTER_HYPHEN.split(part))
    last = len(groups) - 1
    held = "".join(
        _held(group, at == 0, at == last) if at in (0, last) else "".join(group) for at, group in enumerate(groups)
    )
    inner = f'<span class="part">{held}</span>' if boxed else held
    return f'<span class="whole">{inner}</span>'


def _token(word: str, sole: bool) -> str:
    """A word with a seam or a hyphen inside it, as a token. A word with
    neither reads as a word, and stays plain text. A token that is the whole
    value (a name, never a word of a sentence) is marked ``data-sole``, so a
    table cell may cut it rather than break it. A figure among words reads
    as one of them."""

    if not (_INNER_SEAM.search(word) or _INNER_HYPHEN.search(word)):
        return word
    if not sole and _FIGURE.fullmatch(unescape(word)):
        return word
    # A word has no whitespace, so a newline marks where it splits.
    seams = _BREAK_AFTER.sub("\\1\n", word).split("\n")
    # Only a name standing alone holds its end labels: a value of several
    # words (a list, a sentence) shares its line, and a longer piece there
    # widens the column it sits in.
    seams = _labels_held(seams) if sole else seams
    parts = ["".join(group) for group in _glued(seams)]
    if not sole:
        # A word of a phrase: each part is cut on its own, at its cell's edge.
        return f'<span class="token">{_SEAM.join(_whole(part, boxed=True) for part in parts)}</span>'
    inside = _SEAM.join(_whole(part) for part in parts)
    # A name a cell cuts inside a hyphenated part is cut by a box within the
    # token, so the token can ask for the name's whole length around it
    # (figures.css).
    if any(_INNER_HYPHEN.search(part) for part in parts):
        inside = f'<span class="cut">{inside}</span>'
    # The name again, unseen and with nothing to copy, broken only where the
    # name breaks once a row opens it: a table cell that cuts the name sizes
    # its column by it, so the column already holds the longest piece
    # (tables.css). First, so a mark after the name lands in the name.
    strut = f'<span class="strut" data-strut="{"&#8203;".join(parts)}" aria-hidden="true"></span>'
    return f'{strut}<span class="token" data-sole{_short(word)}>{inside}</span>'


# The characters a value a cell cuts holds out for (`--cut-floor`,
# tables.css) in a narrow box and in any other, counted generously: a
# proportional face sets most letters narrower than `ch`.
_FLOORS = (6, 16)


def _short(word: str) -> str:
    """``data-short``, naming the least floor a name is no longer than: such a
    name holds out for all of it, and no more, since a box padded to the floor
    leaves a gulf before the words after it on its line."""

    length = len(unescape(word))
    floor = next((floor for floor in _FLOORS if length <= floor), None)
    return f' data-short="{floor}"' if floor else ""


# The tags that close a word's boxes, after its last letter.
_CLOSING = re.compile(r"(?:</span>)*$")


@register.filter(is_safe=True)
def breakable(value, mark=""):
    """``value`` as tokens, one per word: escaped, with a line-break
    opportunity where an address or a path may wrap, and none inside a
    hyphenated part.

    The one way a template writes an identifier (``<code>{{ x|breakable }}</code>``,
    or bare as a link's text). A token that fits on its line moves to the next
    one whole; one longer than its line wraps at its seams; a part longer than
    its line alone breaks wherever it must. Markup passes as it is: what built
    it held the names in it, and a seam inside a tag would break the tag.

    ``mark`` is the punctuation the sentence puts after the value
    (``{{ x|breakable:"." }}``), written inside the last word's innermost box:
    a box as wide as its line leaves a mark after it no room there, and the
    mark would start the next line alone. Given the ``forloop`` of a list
    (``{{ x|breakable:forloop }}``), the mark is the comma between its items.
    """

    mark = _mark(mark)
    value = admitted(value)
    # Markup is whatever renders itself: a form field is not SafeData until
    # Django renders it.
    if hasattr(value, "__html__"):
        return conditional_escape(value) + conditional_escape(mark)
    # Escaped first, so the only markup in the result is what is added here.
    text = str(conditional_escape(value))
    words = _WORDS.split(text)
    drawn = [_token(word, sole=len(words) == 1) for word in words]
    if mark:
        last = drawn[-1]
        at = _CLOSING.search(last).start()
        # Hidden from the accessibility tree: in a link it would be heard as
        # part of the link's name.
        drawn[-1] = f'{last[:at]}<span class="mark" aria-hidden="true">{conditional_escape(mark)}</span>{last[at:]}'
    return mark_safe("".join(drawn))


def _mark(mark) -> str:
    """A mark as ``breakable`` takes it: the text given, or for a list's
    ``forloop`` a comma after every item but the last."""

    if isinstance(mark, dict):
        return "" if mark["last"] else ","
    return str(mark)


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

    return getattr(context, "request", None) or context.get("request")


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
            _mark(mark)
        )
    if label:
        link = replace(link, label=str(label))
    label = format_html("<code>{}</code>", breakable(link.label, mark)) if code else breakable(link.label, mark)
    if link.url and not link.external and _is_this_page(context, link.url):
        return format_html('<span data-entity="{}">{}</span>', link.kind_label, label)
    if link.url and link.external:
        return mark_safe(
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
    ``url`` is given: its names held as ``breakable`` holds them, the whole
    line in its title where the cell cuts it. The line asks for its whole
    length around the box that cuts it (``.cut``, figures.css)."""

    if url:
        return format_html(
            '<a class="cell-line" href="{}" title="{}"><span class="cut">{}</span></a>', url, text, breakable(text)
        )
    return format_html('<span class="cell-line" title="{}"><span class="cut">{}</span></span>', text, breakable(text))


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
def referenced_by(context, thing, identity="", cards=True):
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
def kind_label(kind):
    """What a registry kind is called in a sentence: ``tailscale.device`` reads "Tailnet device"."""

    return label(str(kind or ""))


@register.filter
def entity_of(kind, identity):
    """``{% entity "machine"|entity_of:name %}``: the link builder's answer for one thing."""

    return entity_link(str(kind), str(identity or ""))


_UNNAMED = object()


@register.filter
def declared_link(link, label=_UNNAMED):
    """A link an emitter declared (a label and a url), as an entity mention.

    ``{% entity url|declared_link:label %}`` names a page outside HQ: a url
    that is not a web address leaves the label unlinked, so a page builds no
    href from data HQ did not write (``web_url``)."""

    if label is _UNNAMED:
        return declared(link.label, link.url)
    return declared(str(label), _web_url(link), kind="dependency")


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
def where_it_runs(public_url):
    """The service, machine and container behind a published address, for
    ``partials/_runs_on.html``; nothing when no service answers there."""
    return _where_it_runs(public_url)


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
    return _problems_about(url or (request.path if request is not None else ""))


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

    def __init__(self, partial: str, parts: dict[str, template.NodeList], options: dict) -> None:
        self.partial = partial
        self.options = options
        self.child_nodelists = tuple(f"nodelist_{name}" for name in parts)
        for name, nodelist in parts.items():
            setattr(self, f"nodelist_{name}", nodelist)

    def render(self, context) -> str:
        values = {name: value.resolve(context) for name, value in self.options.items()}
        for attribute in self.child_nodelists:
            # Rendered by the page's own template, so escaped already. A part
            # left empty leaves an option of its name as it is.
            part = mark_safe(getattr(self, attribute).render(context).strip())
            name = attribute.removeprefix("nodelist_")
            if part or name not in values:
                values[name] = part
        return context.template.engine.get_template(self.partial).render(context.new(values)).strip()


def slotted(name: str, partial: str, parts: tuple[str, ...], options: frozenset[str]) -> None:
    """Register ``{% name option=value %}first{% second %}...{% endname %}``:
    markup a page would otherwise write by hand, drawn by ``partial`` with
    each part as the variable it is named for. The first part follows the
    opening tag; the others may be left out, and come in ``parts``' order."""

    end = f"end{name}"

    def compile_(parser, token) -> SlotsNode:
        bits = token.split_contents()
        given = token_kwargs(bits[1:], parser)
        if len(given) != len(bits) - 1 or not set(given) <= options:
            raise template.TemplateSyntaxError(f"{name} takes {', '.join(sorted(options))} as name=value")
        found = {part: template.NodeList() for part in parts}
        ahead = list(parts)
        while True:
            found[ahead.pop(0)] = parser.parse((*ahead, end))
            marker = parser.next_token().contents
            if marker == end:
                return SlotsNode(partial, found, given)
            ahead = ahead[ahead.index(marker) :]

    register.tag(name, compile_)


slotted("section_head", "partials/_section_head.html", ("heading", "aside"), frozenset(("id", "lede")))
slotted(
    "outside_link",
    OUTSIDE_LINK,
    ("content",),
    frozenset(("url", "css", "tone", "tip", "title", "label", "modal", "entity")),
)
slotted("band_fact", "partials/_band_fact.html", ("value", "tone"), frozenset(("icon", "label", "tone")))
