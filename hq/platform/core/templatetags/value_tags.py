import re
from datetime import date, datetime
from urllib.parse import unquote, urlsplit

from django import template
from django.utils import timezone
from django.utils.html import format_html

from hq.platform.application.entity_links import web_url as _web_url
from hq.platform.application.labels import human_bytes
from hq.platform.application.moments import (
    ago as _ago,
    elapsed as _elapsed,
    when as _when,
    when_day as _when_day,
    when_exact as _when_exact,
)
from hq.platform.application.money import money as _money
from hq.platform.application.timestamps import moment
from hq.platform.application.ui import MISSING, counted as _counted

register = template.Library()


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
    a value and is shown; only None and the empty string are missing.
    """
    if value is None or value == "":
        return format_html('<span class="empty-value" title="None">{}</span>', MISSING)
    return value


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


# What ``when`` can be asked for, and the words each one shows.
_WHEN_FORMS = {
    "": _when,
    "ago": lambda value: _ago(value) if isinstance(value, datetime) else _when_day(value),
    "day": _when_day,
    "exact": _when_exact,
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

    Takes a datetime, a date or an ISO stamp. Nothing shows the mark for a
    missing value; text that names no instant is shown as it came.
    """

    if form not in _WHEN_FORMS:
        raise template.TemplateSyntaxError(
            f"when takes {', '.join(repr(name) for name in _WHEN_FORMS if name)} or nothing; got {form!r}."
        )
    if value is None or value == "":
        return or_empty(value)
    found = _instant(value)
    if found is None:
        return value
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
def signed_in(request):
    """``{{ request|signed_in }}``: how long ago this session's sign-in was, in hours or days.

    Never minutes: a menu that counts them is a clock nobody asked for. The
    session says when its sign-in was; one that does not (a password sign-in)
    falls back on the account's last login.
    """
    from hq.platform.core.oidc import SIGNED_IN_SESSION_KEY

    session = getattr(request, "session", None)
    when = moment(str(session.get(SIGNED_IN_SESSION_KEY) or "")) if session is not None else None
    when = when or getattr(getattr(request, "user", None), "last_login", None)
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
    from hq.platform.application.moments import span as said

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
    from hq.domains.control_plane.provider_spec import expiry_phrase

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
    """A value that may be an ISO 8601 timestamp: ``when`` if it is, unchanged if not."""

    if not isinstance(value, str) or "T" not in value:
        return value
    return when(value)


def _is_this_page(context, url: str) -> bool:
    """Whether ``url`` is the page being rendered, not a fragment of it."""

    request = context.get("request")
    if request is None or not url or "#" in url:
        return False
    parts = urlsplit(url)
    return not parts.netloc and unquote(parts.path) == request.path


@register.simple_tag(takes_context=True)
def entity(context, link, code=False, label=""):
    """One entity name, as the link builder answered for it.

    ``link`` is an ``application.entity_links.EntityLink``. Every mention of an
    entity on a page renders here, marked ``data-entity``. An external link
    opens in a new tab without a way back to this page. A link to the page it
    is on renders as plain text.
    """
    if link is None:
        return format_html('<span class="empty-value" title="None">{}</span>', MISSING)
    if label:
        from dataclasses import replace

        link = replace(link, label=str(label))
    label = format_html("<code>{}</code>", link.label) if code else link.label
    if link.url and not link.external and _is_this_page(context, link.url):
        return format_html('<span data-entity="{}">{}</span>', link.kind_label, label)
    if link.url and link.external:
        return format_html(
            '<a href="{}" data-entity="{}" class="external-link" target="_blank" '
            'rel="noopener noreferrer" title="Opens {}">{}</a>',
            link.url,
            link.kind_label,
            link.kind_label,
            label,
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


def _viewer(context):
    """Who the page is rendered for, as the application asks about them."""

    from hq.platform.application.security import web_principal

    return web_principal(context["request"].user)


@register.simple_tag(takes_context=True)
def reference(context, row, field):
    """``{% reference expense "paid_from" as account %}``: the link for the
    reference a row holds, for ``{% entity %}``. None when it holds none, or
    one this viewer may not be told about."""

    from hq.platform.application.references import reference_of

    return reference_of(row, field, principal=_viewer(context))


@register.inclusion_tag("partials/_referenced_by.html", takes_context=True)
def referenced_by(context, thing, identity="", cards=False):
    """Everything that names a thing, for that thing's page.

    ``{% referenced_by vehicle %}`` for a row of a model that can be referred
    to; ``{% referenced_by "machine" machine.name %}`` for a kind and what its
    page is addressed by. ``cards=True`` draws each list as a card. One
    statement, whatever names it.
    """

    from hq.platform.application import references

    principal = _viewer(context)
    if isinstance(thing, str):
        found = references.referenced_by(thing, str(identity), principal=principal)
    else:
        found = references.referenced_by_row(thing, principal=principal)
    return {"mentions": found, "cards": cards, "request": context.get("request")}


@register.filter
def kind_label(kind):
    """What a registry kind is called in a sentence: ``tailscale.device`` reads "Tailnet device"."""
    from hq.platform.application.entity_links import kind_label as label

    return label(str(kind or ""))


@register.filter
def entity_of(kind, identity):
    """``{% entity "machine"|entity_of:name %}``: the link builder's answer for one thing."""
    from hq.platform.application.entity_links import entity_link

    return entity_link(str(kind), str(identity or ""))


@register.filter
def declared_link(link):
    """A link an emitter declared (a label and a url), as an entity mention."""
    from hq.platform.application.entity_links import declared_link as declared

    return declared(link.label, link.url)


@register.filter
def connection_anchor(ref):
    """The id of a connection's row, the target of its entity link."""
    from hq.platform.application.entity_links import connection_anchor as anchor

    return anchor(str(ref or ""))


# An identifier: a key, a hostname, an address, a path, a port spec. Anything
# with a character plain prose does not use.
_IDENTIFIER = re.compile(r"[-_./:@=#]")


@register.filter
def readout(value):
    """A value as a readout shows it: an identifier in code, anything a person
    would write in a sentence (a count, a word, a phrase) as text.

    Every value in code made "11", "On" and "Need approval" look like keys to
    copy, and put a grey chip around each number in a list of three numbers.
    """

    text = str(value)
    if " " not in text.strip() and _IDENTIFIER.search(text):
        return format_html("<code>{}</code>", text)
    return text


@register.filter
def web_url(value):
    """An href from data someone else wrote: http(s) or nothing."""

    return _web_url(value)


@register.filter
def where_it_runs(public_url):
    """The service, machine and container behind a published address, for
    ``partials/_runs_on.html``; nothing when no service answers there."""
    from hq.platform.application.where_it_runs import where_it_runs as found

    return found(public_url)


@register.filter
def named_links(text):
    """Free text as plain stretches and the links of the names HQ knows in
    it, for ``partials/_linked_text.html``."""
    from hq.platform.application.where_it_runs import named_links as found

    return found(text)


@register.simple_tag(takes_context=True)
def problems_about(context, url=""):
    """The open problems about the thing at ``url``, by default the page this
    is drawn on, for ``partials/_problems.html``. One read of a stored answer,
    made only by a page that asks."""
    from hq.platform.application.problems import problems_about as found

    request = context.get("request")
    return found(url or (request.path if request is not None else ""))
