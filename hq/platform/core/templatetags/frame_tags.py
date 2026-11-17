"""The parts that frame what a page writes inside them: a card, muted
words, a row of buttons, the content column, a list of steps or sentences,
a row of chips, and a row of readings."""

from django import template
from django.utils.safestring import mark_safe

from hq.platform.application.page_parts import html_attributes
from hq.platform.application.tone import Tone
from hq.platform.core.included_parts import PATH_HOPS, READING_AGE
from hq.platform.core.template_parts import Option, Part, slotted
from hq.platform.core.templatetags.facts_tags import FACT

register = template.Library()


def framed(given: dict, own: str, named: dict) -> dict:
    """A part's opening and closing tags, its ``element`` the one given (a
    choice its option holds it to), its class ``own`` before the page's
    ``css``, and each ``data_*`` option an attribute. Built here rather than
    in its partial, since the element is a choice and every attribute but the
    class is drawn only when it has a value, as ``html_attributes`` draws one."""

    element = given["element"]
    named = {"class": " ".join((own, given.get("css") or "")).strip(), **named}
    named |= {option.replace("_", "-"): value for option, value in given.items() if option.startswith("data_")}
    # The element is one of its option's choices, a tag name and never text,
    # and html_attributes escapes the attributes.
    return {
        "opening": mark_safe(f"<{element}{html_attributes(named)}>"),  # noqa: S308 - see above
        "closing": mark_safe(f"</{element}>"),  # noqa: S308 - see above
    }


def card_frame(_name: str, given: dict) -> dict:
    """A card's opening and closing tags."""

    named = {
        "id": given.get("id"),
        "aria-labelledby": given.get("heading"),
        "aria-label": given.get("label"),
        "aria-busy": "true" if given.get("busy") else None,
        "open": True if given.get("open") else None,
        "data-alone": True if given.get("alone") else None,
    }
    return framed(given, "card", named)


def muted_frame(_name: str, given: dict) -> dict:
    """Muted words' opening and closing tags."""

    tip = given.get("tip")
    named = {
        "id": given.get("id"),
        "title": given.get("title"),
        "role": given.get("role"),
        "hidden": True if given.get("hidden") else None,
        "tabindex": "0" if tip else None,
        "data-tip": tip,
    }
    return framed(given, "muted", named)


MUTED = Part(
    name="muted",
    classes=("muted",),
    partial="partials/_muted.html",
    about=(
        "Words said quietly beside what they are about: a context line, a sentence under a value. "
        "A cell or a list item holds it; a run of parts said quietly is `{% dot_list muted=True %}`."
    ),
    options={
        "element": Option(
            default="span", choices=("span", "p", "div"), about="A line among other words, a paragraph or a block."
        ),
        "css": Option(about="The classes of where it stands (`narrow-more`, `row-more`, `cell-part`)."),
        "id": Option(about="Its `id`."),
        "title": Option(about="Its `title`."),
        "tip": Option(about="What a press or a hover shows, as a status's tip is."),
        "hidden": Option(about="Drawn hidden, for a line a script shows."),
        "role": Option(about="Its `role`, for a line a script shows and a reader hears."),
    },
    data=True,
    public=True,
    example='{% muted element="p" %}A sentence under it.{% endmuted %}',
    shape=muted_frame,
)
slotted(MUTED, library=register)
slotted(
    library=register,
    part=Part(
        name="steps",
        example='{% steps %}{% step icon="check-circle" tone="good" %}Example check holds.{% endstep %}{% endsteps %}',
        classes=("is-numbered", "step-list"),
        partial="partials/_steps.html",
        about="A list of short steps or checks, each a `{% step %}`, read down its columns.",
        options={"numbered": Option(about="Steps done in order: one column, each its number in place of its icon.")},
    ),
)
slotted(
    library=register,
    part=Part(
        name="step",
        example="{% steps numbered=True %}{% step done=True %}Pull the example image.{% endstep %}{% step %}Start it.{% endstep %}{% endsteps %}",
        classes=("is-done",),
        draws=(FACT,),
        partial="partials/_step.html",
        about="One step or check of a `{% steps %}` list: its icon, or its number, then what it says.",
        options={
            "icon": Option(about="The icon before what it says."),
            "tone": Option(about="How the step stands, the color of its icon."),
            "done": Option(about="A finished step, drawn quiet."),
            "title": Option(about="Its tooltip."),
        },
    ),
)
slotted(
    library=register,
    part=Part(
        name="prose_list",
        classes=("prose-list",),
        partial="partials/_prose_list.html",
        about="A list of sentences, each an `<li>` marked as the browser marks it.",
        options={"numbered": Option(about="Each numbered, for items read in order.")},
        public=True,
        example="{% prose_list %}<li>A sentence.</li>{% endprose_list %}",
    ),
)
slotted(
    library=register,
    part=Part(
        name="link_chips",
        example='{% link_chips %}<li><a href="/example/">Example page</a></li>{% endlink_chips %}',
        classes=("link-chips",),
        partial="partials/_link_chips.html",
        about="A row of places to go, each `<li>` one link drawn as a chip of its own.",
    ),
)
slotted(
    library=register,
    part=Part(
        name="button_row",
        classes=("form-actions",),
        public_classes=("save-bar",),
        partial="partials/_button_row.html",
        about="A row of buttons that stands on its own: a form's closing row, a card's decisions, a page's way on.",
        options={"bar": Option(about="Pinned to the bottom of the screen once its form has changed.")},
        public=True,
        example='{% button_row %}{% button "Save" submit=True look="primary" %}{% endbutton_row %}',
    ),
)
slotted(
    library=register,
    part=Part(
        name="page_main",
        example="{% page_main %}<h1>Example page</h1><p>An example line.</p>{% endpage_main %}",
        classes=("content", "page-print-footer"),
        partial="partials/_page_main.html",
        about="The content column every page and each catalog entry is drawn in, a region answered in place.",
        parts=("content", "print_footer"),
    ),
)
slotted(
    library=register,
    part=Part(
        name="card",
        partial="partials/_card.html",
        about="The frame a page's content is grouped in; a titled part of a page is a `{% section %}`.",
        options={
            "element": Option(
                default="section",
                choices=("section", "article", "figure", "details", "div"),
                about="The element it is.",
            ),
            "id": Option(about="Its `id`."),
            "css": Option(about="The classes of where it stands."),
            "heading": Option(about="The id of the heading that names it."),
            "label": Option(about="What it holds, for a card with no heading."),
            "busy": Option(about="Its content is still coming."),
            "open": Option(about="A `details` card drawn open."),
            "alone": Option(about="The whole of a page that stands outside the page frame (sign-in)."),
        },
        data=True,
        public=True,
        example='{% card label="Hosts" %}<p>Body</p>{% endcard %}',
        shape=card_frame,
    ),
)


def reading_frame(_name: str, given: dict) -> dict:
    """A reading's options as its partial draws them: its tone as the shared
    tone class, and each ``data_*`` option as an attribute of the reading."""

    tone = Tone(given["tone"] or Tone.NEUTRAL)
    data = {option.replace("_", "-"): value for option, value in given.items() if option.startswith("data_")}
    return {
        **{option: value for option, value in given.items() if not option.startswith("data_")},
        "tone_class": f" tone-{tone}" if tone is not Tone.NEUTRAL else "",
        "attributes": html_attributes(data),
    }


slotted(
    library=register,
    part=Part(
        name="readings",
        example='{% readings %}{% reading icon="activity" url="/example/" unit="hosts" %}3{% lines %}Example hosts online{% endreading %}{% endreadings %}',
        classes=("readings",),
        partial="partials/_readings.html",
        about="A row of readings, each a `{% reading %}`, spaced as equals rather than ruled apart.",
    ),
)
slotted(
    library=register,
    part=Part(
        name="reading",
        example='{% readings %}{% reading icon="network" tone="attention" note_tone="stale" %}2{% lines %}Example peers{% note %}Read an hour ago{% endreading %}{% endreadings %}',
        classes=(
            *("reading", "reading-head", "reading-lead", "reading-lines", "reading-mark", "reading-panel"),
            *("reading-panel-foot", "reading-panel-head", "reading-standing"),
        ),
        draws=(PATH_HOPS, READING_AGE),
        partial="partials/_reading.html",
        about=(
            "One reading of a `{% readings %}` row: its mark, its figure, its quiet lines and a note on how it "
            "stands, leading to a page or opening a panel."
        ),
        parts=("lead", "lines", "note", "heading", "panel", "foot"),
        options={
            "icon": Option(about="Its mark."),
            "url": Option(about="The page it leads to."),
            "modal": Option(about="The dialog it opens where script runs."),
            "id": Option(about="The id of the panel it opens in place, given a `panel` part."),
            "title": Option(about="What pointing at its figure says."),
            "unit": Option(about="The words after its figure."),
            "tone": Option(choices=("", *Tone), about="How it stands, in the status tones."),
            "note_tone": Option(
                choices=("", "attention", "stale"), about="How its note stands: a problem, or past its time."
            ),
        },
        data=True,
        shape=reading_frame,
    ),
)
