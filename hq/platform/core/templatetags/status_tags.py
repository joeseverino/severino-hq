"""A status: one short state, drawn in its tone and said in sentence case.

The one way a page draws a state (`{% status %}` in a template, `status_pill`
in Python), so every state reads alike wherever it stands: "Online" and
"Exited" beside "Running", never one page's "online" beside another's "Done".
It is a pill, or with ``pill=False`` its words in its tone alone, as a figure
that says which way it went is drawn among others, or with ``chip=True`` a
neutral chip: one of a record's names (a technology, a domain, a role), said
as written. A source test in
`test_status_tags.py` refuses a pill or tone class written by hand in any
template the host or an extension ships.
"""

from django import template
from django.utils.html import format_html, format_html_join
from django.utils.safestring import SafeString, mark_safe

from hq.platform.application.labels import sentence
from hq.platform.core.templatetags.value_tags import breakable
lazy from hq.platform.application.infrastructure import RESOURCE_TONES
lazy from hq.platform.application.ui import STATUS_VALUES

register = template.Library()


def _tone(tone: str) -> str:
    """The tone class a state is drawn in. A declared resource's health state
    ("healthy", "drifted") is said as the tone every page draws it in; any
    other word names its own class (`pill-attention`, `pill-archived`), and
    one the stylesheet does not color is neutral."""

    return RESOURCE_TONES.get(tone, tone)


def status_pill(
    *parts: object,
    tone: str = "",
    css: str = "",
    href: str = "",
    title: str = "",
    tip: str = "",
    pill: bool = True,
    chip: bool = False,
    **data: object,
) -> SafeString:
    """The pill for a state: ``parts`` its words, the first said in sentence
    case and each held as ``breakable`` holds a value; ``tone`` its color.
    ``pill=False`` draws the words in that color with no pill around them,
    on fixed digit widths. ``chip=True`` draws a name a record carries,
    in no tone and in its own case: "django" and "example.com" stay as
    written.

    ``href`` makes it a link, ``title`` names it on hover, ``tip`` gives it
    HQ's tooltip (and the focus a tooltip needs), ``css`` adds classes (a
    queue row's ``is-mark``), and each ``data_*`` keyword is a ``data-``
    attribute.
    """

    if chip and (tone or not pill):
        raise ValueError("A chip is a neutral name, never a state: it takes no tone and no pill=False.")
    words = [str(part) for part in parts if part not in (None, "")]
    if words and not chip:
        words[0] = sentence(words[0])
    label = mark_safe(" ".join(str(breakable(word)) for word in words))  # noqa: S308 - each word is escaped by breakable
    tone_class = _tone(tone)
    if chip:
        classes = "tag"
    elif not pill:
        # The status vocabulary's tones are the only ones words alone take.
        # Written whole, so the stylesheet's usage check reads the `toned-` family.
        classes = f"toned toned-{tone_class if tone_class in STATUS_VALUES else 'neutral'}"
    else:
        # Written whole, so the stylesheet's usage check reads the `pill-` family.
        classes = f"pill pill-{tone_class}" if tone_class else "pill"
    if css:
        classes = f"{classes} {css}"
    attributes = [("class", classes)]
    if href:
        attributes.append(("href", href))
    if title:
        attributes.append(("title", title))
    if tip:
        attributes += [("tabindex", "0"), ("data-tip", tip)]
    attributes += [(f"data-{name.removeprefix('data_').replace('_', '-')}", value) for name, value in data.items()]
    drawn = format_html_join("", ' {}="{}"', attributes)
    element = "a" if href else "span"
    return format_html("<{}{}>{}</{}>", element, drawn, label, element)


# An advisory's severity as the tone its pill takes, wherever one is listed.
SEVERITY_TONES = {"critical": "serious", "high": "serious", "medium": "attention", "moderate": "attention"}


@register.filter
def severity_tone(severity):
    """``{{ advisory.severity|severity_tone }}``: the tone of an advisory's
    severity, critical and high serious, medium attention, the rest neutral."""

    return SEVERITY_TONES.get(str(severity or "").lower(), "neutral")


def _field(thing, name: str) -> str:
    """A queue item's field, from its projection (a mapping) or a record."""

    value = thing.get(name) if isinstance(thing, dict) else getattr(thing, name, None)
    return value if isinstance(value, dict) else str(value or "")


@register.filter
def urgency_tone(item):
    """A queue item's tone: a notice reads as news, the rest as how urgent."""

    return "idea" if _field(item, "notice") not in ("", "False") else _field(item, "status")


@register.filter
def origin_tip(item, mark=""):
    """What a queue item's urgency says on hover: how urgent on a row's mark
    (``item|origin_tip:"mark"``), then what raised it."""

    origin = _field(item, "origin") or {}
    rule, where = _field(origin, "rule"), _field(origin, "where")
    if rule:
        at = f", defined at {where.removeprefix('hq/platform/application/')}" if where else ""
        source = f"Rule {rule}{at}"
    else:
        source = f"From {_field(origin, 'source')}"
    return f"{f'{_field(item, "urgency")}. ' if mark else ''}{source}. ⌘-click for more."


@register.filter
def drift_tip(reading):
    """A drifted reading in a sentence: what HQ expects and what is live."""

    return f"{reading.label}: HQ expects {reading.desired}, live is {reading.value}"


@register.simple_tag(name="status")
def status(*parts, tone="", css="", href="", title="", tip="", pill=True, chip=False, **data):
    """``{% status row.get_status_display tone=row.status %}``: a state as a pill,
    or with ``pill=False`` as its words in its tone (``{% status change tone="good" pill=False %}``),
    or with ``chip=True`` as a neutral chip for a name (``{% status domain chip=True %}``).

    Several parts are one label ("HQ expects" and the value it expects), only
    the first sentence-cased.
    """

    return status_pill(
        *parts,
        tone=str(tone or ""),
        css=css,
        href=str(href or ""),
        title=str(title or ""),
        tip=str(tip or ""),
        pill=bool(pill),
        chip=bool(chip),
        **data,
    )
