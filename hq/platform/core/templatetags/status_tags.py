"""A status: one short state, drawn in its tone and said in sentence case.

The one way a page draws a state (`{% status %}`, `status_pill` in Python), so
every state reads alike wherever it stands. `test_status_tags.py` refuses a
pill or tone class written by hand in any shipped template.
"""

from typing import Annotated, Any, Literal, get_args

from django import template
from django.template.loader import render_to_string
from django.utils.safestring import SafeString, mark_safe

from hq.platform.application.describe import Describe
from hq.platform.application.labels import sentence
from hq.platform.application.page_parts import html_attributes
from hq.platform.application.tone import Absent, Tone, Toned
from hq.platform.core.held_values import format_html
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

    if chip:
        return "tag"
    toned = _TONE_CLASSES[_toned(tone_class)]
    if dot and ink == "context":
        return f"toned {toned} toned-dot toned-ink-context"
    if dot:
        return f"toned {toned} toned-dot"
    if ink == "context":
        return f"toned {toned} toned-ink-context"
    if not pill:
        return f"toned {toned}"
    return f"pill {toned}"


def _refuse_mixed(*, tone: str, pill: bool, chip: bool, dot: bool, ink: str, icon: str, shown: bool) -> None:
    """Refuse options no one form takes together: a toned chip, a mark beside
    a pill or a dot, context ink with neither a dot nor a mark, unshown words
    with no mark to stand for them."""

    if chip and (tone or not pill or dot):
        raise ValueError("A chip is a neutral name, never a state: it takes no tone, no pill=False and no dot.")
    if icon and (pill or dot or chip):
        raise ValueError("An icon stands where a dot would, before a state's words: only beside pill=False.")
    if not shown and not icon:
        raise ValueError("A state's words go unshown only where its icon stands for them: shown=False beside icon=.")
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
    shown: bool = True,
    **data: object,
) -> SafeString:
    """The pill for a state: ``parts`` its words, the first in sentence case; ``tone`` its color, a ``Toned``'s own.

    ``pill=False`` draws the words in that color alone, ``dot=True`` after a
    steady dot, ``icon`` after a mark (partials/_icon.html), and
    ``ink="context"`` leaves the words in the surrounding ink so only the dot
    or mark is toned. ``shown=False`` beside an icon draws the mark alone,
    its words said only to a screen reader. ``chip=True`` draws a record's
    name in no tone and its own case.
    """

    if not tone and parts and isinstance(parts[0], Toned):
        tone = parts[0]
    _refuse_mixed(tone=tone, pill=pill, chip=chip, dot=dot, ink=ink, icon=icon, shown=shown)
    words = [str(part.label if isinstance(part, Toned) else part) for part in parts if part not in (None, "")]
    if words and not chip:
        words[0] = sentence(words[0])
    label = mark_safe(" ".join(str(breakable(word)) for word in words))  # noqa: S308 - each word is escaped by breakable
    if not shown:
        label = format_html('<span class="visually-hidden">{}</span>', label)
    if icon:
        mark = render_to_string("partials/_icon.html", {"name": icon}).strip()
        label = mark_safe(f"{mark} {label}")  # noqa: S308 - the icon part's own markup, and the escaped words
    classes = _form(_tone(tone), pill=pill, chip=chip, dot=dot, ink=ink)
    if not shown:
        classes = f"{classes} toned-alone"
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
def absent(words):
    """``{% status "not set up"|absent %}``: what is not there yet, in its one
    wording (``tone.Absent``); words of any other wording are refused."""

    try:
        return Absent(words)
    except ValueError:
        raise ValueError(
            f"An absence is said as one of {', '.join(repr(str(member)) for member in Absent)}; got {words!r}."
        ) from None


@register.filter
def severity_tone(severity):
    """``{{ advisory.severity|severity_tone }}``: the tone of an advisory's
    severity (``application.ui.severity_tone``)."""

    return _severity_tone(severity)


# Each written whole, so the stylesheet's usage check finds it: the class
# that sets a part's tone roles (tokens.css), a pill's and a frame's alike.
_TONE_CLASSES = {
    Tone.GOOD: "tone-good",
    Tone.ATTENTION: "tone-attention",
    Tone.SERIOUS: "tone-serious",
    Tone.NEUTRAL: "tone-neutral",
}


@register.filter
def tone_class(tone):
    """``class="frame {{ tone|tone_class }}"``: the class that sets a part's
    tone roles (``--tone-mark`` and kin) to a state's tone, for a frame or a line drawn in the color the
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
    shown: Annotated[bool, Describe("Its words shown; with `False`, its icon alone, the words said aloud.")] = True,
    **data: Annotated[Any, Describe("Each a `data-` attribute.")],
):
    """``{% status "Blocked" tone="attention" %}``: a state as a pill in a
    Tone, or a subject's state in its own words and tone (``{% status
    machine.state %}``).

    ``pill``, ``dot``, ``ink``, ``icon`` and ``chip`` are ``status_pill``'s
    (``{% status "Allowed" tone="good" dot=True %}``). Several parts are one
    label, only the first sentence-cased.
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
        shown=bool(shown),
        **data,
    )


slotted(
    Part(
        name="status",
        rules="""
        **A state is a pill drawn by `{% status %}`.** `{% status row.state %}` (`hq_sdk.ui.status_pill`
        from Python) says the state in sentence case (`labels.sentence`), whatever its source wrote:
        Docker's "exited" reads "Exited" beside a model's "Done". Its color is one of the four tones
        (`good`, `attention`, `serious`, `neutral`), never a word of its own: `tone` takes a `Tone`, and a
        subject's state is a `Toned` member that carries its words and its tone (a machine's
        `MachineState`, a model's stored status declared as `TonedChoices`, `ARCHIVED = "archived",
        Tone.NEUTRAL, "Archived"`). Any other word is refused where it is drawn. Several parts are one
        label (`{% status "HQ expects" desired tone="serious" %}`); `href`, `title`, `tip`, `css` and
        `data_*` say the rest. No template or module writes a pill by hand. A pill or a chip sets its own
        line height (cards.css), so it is one height wherever it stands, in a cell or beside a run of links
        whose line is a target tall; that line keeps its height around it. With `chip=True` it is one of
        a record's names (a technology, a domain, a role) as a neutral chip, said as written and taking no
        tone, so no template writes a `.tag` by hand. With `pill=False` it is the state's words in its
        tone, as a headline or a line of text says a state. The sentence that says what a state means
        takes the same tone beside it. A record's title line is "Working · Matches HQ's config.", both
        green, as parts of one `.dot-list`, so the sentence shares the state's line where it fits and
        wraps under it where it does not. With `icon=` beside `pill=False` a mark (`partials/_icon.html`)
        leads those words in their tone. So a table cell says its state by its mark as well as its
        color, as a container's standing does ("1 serious" after a shield). With `ink="context"` only the
        mark is toned ("Up to date"), and with `shown=False` the mark stands alone, its words said only
        to a screen reader, as a certificate's check after the name it serves is. A first word holding a digit, a version, is said as written. A cell
        that is a state carries its tone (`Cell(word, tone=...)`) and is drawn by it. *Catalog:*
        `status/icon`. *Gates:* the `status-pill-built-by-status-pill` ast-grep rule and
        `test_status_tags.py`, which reads every template root for a hand-written pill or a state's tone
        written as a class.
        """,
        about="One short state, drawn in its tone and said in sentence case, the one way a page draws a state.",
        function=status,
        public=True,
        example='{% status "Blocked" tone="attention" %}',
    ),
    library=register,
)
