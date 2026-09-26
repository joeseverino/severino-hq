from datetime import datetime
from urllib.parse import unquote, urlsplit

from django import template
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.html import format_html

from application.ui import MISSING, ago as _ago, counted as _counted, elapsed as _elapsed

register = template.Library()


@register.filter
def counted(count, phrases):
    """``{{ n|counted:"project needs output,projects need output" }}``.

    One word may stand alone (``{{ n|counted:"change" }}``); a phrase gives both
    forms, comma separated, so its verb agrees too. See ``application.ui.counted``.
    """
    one, _, many = str(phrases).partition(",")
    return _counted(int(count or 0), one.strip(), many.strip() or None)


@register.simple_tag
def empty_value():
    """The mark for a value that is not there, where there is no value to test."""
    return format_html('<span class="empty-value" title="None">{}</span>', MISSING)


@register.filter
def or_empty(value):
    """The value, or the one muted mark for a value that is not there.

    Built in, so every HQ and extension template has it without a load. Zero is
    a value and is shown; only None and the empty string are missing.
    """
    if value is None or value == "":
        return format_html('<span class="empty-value" title="None">{}</span>', MISSING)
    return value


@register.filter
def ago(value):
    """``{{ moment|ago }}``: a datetime or an ISO stamp as an age, in ``application.ui``'s phrasing."""

    if isinstance(value, datetime):
        return _ago(value)
    return _elapsed(str(value or ""))


@register.filter
def readable(value):
    """An ISO 8601 timestamp as a person reads one; anything else unchanged."""

    if not isinstance(value, str) or "T" not in value:
        return value
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    if timezone.is_aware(moment):
        moment = timezone.localtime(moment)
    return date_format(moment, "DATETIME_FORMAT")


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


@register.filter
def kind_label(kind):
    """What a registry kind is called in a sentence: ``tailscale.device`` reads "Tailnet device"."""
    from application.entity_links import kind_label as label

    return label(str(kind or ""))


@register.filter
def entity_of(kind, identity):
    """``{% entity "machine"|entity_of:name %}``: the link builder's answer for one thing."""
    from application.entity_links import entity_link

    return entity_link(str(kind), str(identity or ""))


@register.filter
def declared_link(link):
    """A link an emitter declared (a label and a url), as an entity mention."""
    from application.entity_links import declared_link as declared

    return declared(link.label, link.url)


@register.filter
def connection_anchor(ref):
    """The id of a connection's row, the target of its entity link."""
    from application.entity_links import connection_anchor as anchor

    return anchor(str(ref or ""))
