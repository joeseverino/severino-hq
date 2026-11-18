"""A status: one short state, drawn in its tone and said in sentence case.

The one way a page draws a state (`{% status %}` in a template, `status_pill`
in Python), so every state reads alike wherever it stands: "Online" and
"Exited" beside "Running", never one page's "online" beside another's "Done".
It is a pill, or with ``pill=False`` its words in its tone alone, as a figure
that says which way it went is drawn among others, or with ``dot=True`` those
words after a steady dot in the same tone, as a standing state reads ("Allowed",
"Paused"), its words in the ink around them with ``ink="context"`` where
they are a heading's or a menu's own words, or with ``icon=`` those words
after an icon in the same tone in place of the dot, or with ``chip=True`` a
neutral chip: one of a record's names (a technology, a domain, a role), said
as written. A source test in
`test_status_tags.py` refuses a pill or tone class written by hand in any
template the host or an extension ships.
"""

from typing import Annotated, Any, Literal, get_args

from django import template
from django.template.loader import render_to_string
from django.utils.html import format_html
from django.utils.safestring import SafeString, mark_safe

from hq.platform.application.describe import Describe
from hq.platform.application.labels import sentence
from hq.platform.application.page_parts import html_attributes
from hq.platform.application.tone import Tone, Toned
from hq.platform.core.template_parts import Part, slotted
from hq.platform.core.templatetags.value_tags import breakable
lazy from hq.platform.application.ui import ended, severity_tone as _severity_tone

register = template.Library()

# Whose ink a dotted status's words take: its tone's, or the text around it.
Ink = Literal["tone", "context"]
INKS = frozenset(get_args(Ink))


def _tone(tone: str | Toned) -> str:
    """The tone a state is drawn in: a subject's state (``Toned``: a
    machine's, a declared resource's) its own, else a ``Tone`` by its value,
    else none. A word that is neither (a model's "archived") is refused, so a
    pill's color is only ever one of the four tones."""

    if isinstance(tone, Toned):
        return tone.tone
    if not tone:
        return ""
    try:
        return Tone(tone)
    except ValueError:
        raise ValueError(
            f"A status is drawn in a Tone ({', '.join(Tone)}) or a Toned state; got {tone!r}. "
            "Declare its subject's states as a Toned enum (TonedChoices for a model's) and pass the member."
        ) from None


def _toned(tone_class: str) -> Tone:
    """The tone a state's words take without their pill: no tone is neutral,
    and a word that is no tone is refused."""

    try:
        return Tone(tone_class or Tone.NEUTRAL)
    except ValueError:
        raise ValueError(
            f"A status without its pill is drawn in a tone, one of {', '.join(Tone)}; got {tone_class!r}."
        ) from None


def _form(tone_class: str, *, pill: bool, chip: bool, dot: bool, ink: Ink) -> str:
    """The classes of the form a status takes: a chip, its words in its tone
    (after a dot or an icon, the words in the ink around them when ``ink`` is
    "context"), or a pill."""

    # Each assigned whole to `classes`, so the stylesheet's usage check and the
    # browser gate's selector check read the `toned-` family.
    if chip:
        classes = "tag"
    elif dot and ink == "context":
        classes = f"toned toned-{_toned(tone_class)} toned-dot toned-ink-context"
    elif dot:
        classes = f"toned toned-{_toned(tone_class)} toned-dot"
    elif ink == "context":
        classes = f"toned toned-{_toned(tone_class)} toned-ink-context"
    elif not pill:
        classes = f"toned toned-{_toned(tone_class)}"
    else:
        classes = "pill"
        if tone_class:
            classes = f"{classes} {_PILL_TONES[Tone(tone_class)]}"
    return classes


# Each written whole, so a pill is colored by one of the four tones and the
# stylesheet's usage check reads any other `pill-` rule as one nothing draws.
_PILL_TONES = {
    Tone.GOOD: "pill-good",
    Tone.ATTENTION: "pill-attention",
    Tone.SERIOUS: "pill-serious",
    Tone.NEUTRAL: "pill-neutral",
}


def _refuse_mixed(*, tone: str, pill: bool, chip: bool, dot: bool, ink: str, icon: str) -> None:
    """Refuse options no one form takes together: a toned chip, a mark beside
    a pill or a dot, context ink with neither a dot nor a mark."""

    if chip and (tone or not pill or dot):
        raise ValueError("A chip is a neutral name, never a state: it takes no tone, no pill=False and no dot.")
    if icon and (pill or dot or chip):
        raise ValueError("An icon stands where a dot would, before a state's words: only beside pill=False.")
    if ink not in INKS or (ink == "context" and not (dot or icon)):
        raise ValueError(
            f'ink is one of {", ".join(sorted(INKS))}, and "context" only beside dot=True or an icon; got {ink!r}.'
        )


def status_pill(
    *parts: object,
    tone: str | Toned = "",
    css: str = "",
    href: str = "",
    title: str = "",
    tip: str = "",
    pill: bool = True,
    chip: bool = False,
    dot: bool = False,
    ink: Ink = "tone",
    icon: str = "",
    **data: object,
) -> SafeString:
    """The pill for a state: ``parts`` its words, the first said in sentence
    case and each held as ``breakable`` holds a value; ``tone`` its color,
    a subject's state (``Toned``) its own when it is the first part.
    ``pill=False`` draws the words in that color with no pill around them,
    on fixed digit widths, and ``dot=True`` those words after a dot in that
    color, which never moves; ``ink="context"`` leaves those words in the
    ink around them, so only the dot carries the tone. ``chip=True`` draws
    a name a record carries, in no tone and in its own case: "django" and
    "example.com" stay as written. ``icon`` names a mark (partials/_icon.html)
    drawn before the words of ``pill=False`` in their tone, where the dot
    would stand, so a cell says its state by its mark as well as its color;
    with ``ink="context"`` only the mark takes the tone.

    ``href`` makes it a link, ``title`` names it on hover, ``tip`` gives it
    HQ's tooltip (and the focus a tooltip needs), ``css`` adds classes (a
    queue row's ``is-mark``), and each ``data_*`` keyword is a ``data-``
    attribute, drawn as ``html_attributes`` draws one.
    """

    if not tone and parts and isinstance(parts[0], Toned):
        tone = parts[0]
    _refuse_mixed(tone=tone, pill=pill, chip=chip, dot=dot, ink=ink, icon=icon)
    words = [str(part.label if isinstance(part, Toned) else part) for part in parts if part not in (None, "")]
    if words and not chip:
        words[0] = sentence(words[0])
    label = mark_safe(" ".join(str(breakable(word)) for word in words))  # noqa: S308 - each word is escaped by breakable
    if icon:
        mark = render_to_string("partials/_icon.html", {"name": icon}).strip()
        label = mark_safe(f"{mark} {label}")  # noqa: S308 - the icon part's own markup, and the escaped words
    classes = _form(_tone(tone), pill=pill, chip=chip, dot=dot, ink=ink)
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
    element = "a" if href else "span"
    return format_html("<{}{}>{}</{}>", element, html_attributes(dict(attributes)), label, element)


@register.filter
def severity_tone(severity):
    """``{{ advisory.severity|severity_tone }}``: the tone of an advisory's
    severity (``application.ui.severity_tone``)."""

    return _severity_tone(severity)


# Each written whole, so the stylesheet's usage check finds it.
_TONE_CLASSES = {
    Tone.GOOD: "toned-good",
    Tone.ATTENTION: "toned-attention",
    Tone.SERIOUS: "toned-serious",
    Tone.NEUTRAL: "toned-neutral",
}


@register.filter
def tone_class(tone):
    """``class="frame {{ tone|tone_class }}"``: the class that sets a part's
    ``--toned`` to a state's tone, for a frame or a line drawn in the color the
    state's `{% status %}` takes: no tone is neutral, and a word that is no
    tone is refused, as a status's is."""

    return _TONE_CLASSES[_toned(str(tone or ""))]


def _field(thing, name: str) -> str:
    """A queue item's field, from its projection (a mapping) or a record."""

    value = thing.get(name) if isinstance(thing, dict) else getattr(thing, name, None)
    return value if isinstance(value, dict) else str(value or "")


@register.filter
def urgency_tone(item):
    """A queue item's tone: a notice reads as news, the rest as how urgent."""

    return Tone.NEUTRAL if _field(item, "notice") not in ("", "False") else _field(item, "status")


@register.filter
def origin_tip(item, mark=""):
    """What a queue item's urgency says on hover: how urgent on a row's mark
    (``item|origin_tip:"mark"``), then why, as its panel leads with it."""

    why = ended(_field(_field(item, "origin") or {}, "why"))
    return f"{f'{_field(item, "urgency")}. ' if mark else ''}{why} ⌘-click for more."


@register.filter
def drift_tip(reading):
    """A drifted reading in a sentence: what HQ expects and what is live."""

    return f"{reading.label}: HQ expects {reading.desired}, live is {reading.value}"


def status(
    *parts: Annotated[Any, Describe("Its words, several being one label, only the first sentence-cased.")],
    tone: Annotated[Any, Describe("Its tone, or a subject's state in its own words and tone.")] = "",
    css: Annotated[str, Describe("The classes of where it stands.")] = "",
    href: Annotated[Any, Describe("Where it leads.")] = "",
    title: Annotated[Any, Describe("Its `title`.")] = "",
    tip: Annotated[Any, Describe("What a press or a hover shows.")] = "",
    pill: Annotated[bool, Describe("A pill; with `False`, its words in its tone.")] = True,
    chip: Annotated[bool, Describe("A neutral chip for a name, never a state.")] = False,
    dot: Annotated[bool, Describe("Its words after a steady dot in its tone.")] = False,
    ink: Annotated[
        Ink, Describe("Whose ink its words take beside a dot or an icon: its tone's or the text's.")
    ] = "tone",
    icon: Annotated[str, Describe("Its words after an icon in its tone, beside `pill=False`.")] = "",
    **data: Annotated[Any, Describe("Each a `data-` attribute.")],
):
    """``{% status "Blocked" tone="attention" %}``: a state as a pill in a Tone,
    a subject's state in its own words and tone (``{% status machine.state %}``),
    or with ``pill=False`` as its words in its tone (``{% status change tone="good" pill=False %}``),
    or with ``dot=True`` as those words after a steady dot (``{% status "Allowed" tone="good" dot=True %}``),
    with ``ink="context"`` the words in the ink around them and only the dot toned,
    or with ``icon=`` after a mark in its tone (``{% status "1 serious" tone="serious" pill=False icon="shield" %}``),
    or with ``chip=True`` as a neutral chip for a name (``{% status domain chip=True %}``).

    Several parts are one label ("HQ expects" and the value it expects), only
    the first sentence-cased.
    """

    return status_pill(
        *parts,
        tone=tone if isinstance(tone, Toned) else str(tone or ""),
        css=css,
        href=str(href or ""),
        title=str(title or ""),
        tip=str(tip or ""),
        pill=bool(pill),
        chip=bool(chip),
        dot=bool(dot),
        ink=ink,
        icon=str(icon or ""),
        **data,
    )


slotted(
    Part(
        name="status",
        about="One short state, drawn in its tone and said in sentence case, the one way a page draws a state.",
        function=status,
        public=True,
        example='{% status "Blocked" tone="attention" %}',
    ),
    library=register,
)
