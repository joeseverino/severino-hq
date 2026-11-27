"""The parts that frame what a page writes inside them: a card, muted
words, a row of buttons, the content column, a list of steps or sentences,
and a row of chips."""

from django import template
from django.utils.safestring import mark_safe

from hq.platform.application.page_parts import html_attributes
from hq.platform.application.tone import tone_of
from hq.platform.core.part_examples import sequence, tags
from hq.platform.core.template_parts import Option, Part, slotted
from hq.platform.core.templatetags.facts_tags import FACT
from hq.platform.core.templatetags.icon_tags import drawn

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
    rules="""
    Words in the muted voice that name nothing (a context line beside a value, a sentence under it)
    are `{% muted %}...{% endmuted %}`, a line among other words, or a paragraph with `element="p"`;
    `css` adds the classes of where it stands (`narrow-more`, `row-more`, `cell-part`). What names a
    value (`.fact-label`, a tile's label, a `<dt>`) is the same voice, drawn by its own part.
    """,
    classes=("muted",),
    partial="partials/_muted.html",
    about=(
        "Words said quietly beside what they are about: a context line, a sentence under a value. "
        'A cell or a list item holds it; a run of parts said quietly is `{% list kind="dots" muted=True %}`.'
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
    states=tags.MUTED_STATES,
)
slotted(MUTED, library=register)


def muted(content: str, **given) -> str:
    """Words drawn already, muted from Python as ``{% muted %}`` mutes them:
    its partial writes the frame its shape builds around them."""

    frame = MUTED.values(given)
    return mark_safe(f"{frame['opening']}{content.strip()}{frame['closing']}")  # noqa: S308 - escaped by what drew each


slotted(
    library=register,
    part=Part(
        name="steps",
        rules="""
        A list of short steps (`{% steps %}`) is laid out in rows of as many columns as fit, up to four, so
        no column stands empty beside a long step. Each step is `{% step icon=... %}`: its icon, then what it says; `tone` colors the
        icon (a numbered step's number) by how a check stands, `done` draws a finished step quiet, and `title` is its tooltip. Steps
        done in order are `{% steps numbered=True %}`: wider columns, as many as fit up to three, so a short run fills a wide frame; each step's number stands where its icon would be, so
        its steps take no icon. A step an operator takes on their own machine (a `ByHand`) or in words (a
        `Do`), each with its notes, is `partials/_operator_step.html` given `step`: HQ offers it and never
        runs it, and it carries no secret.

        A workflow HQ declares (`sequences.SequenceSpec`) is drawn whole by `{% steps of=sequence %}`,
        which encloses nothing: each step numbered, its title linked to the page that holds it, its state's
        word, its sentence, then each of its parts (a check, an app) on a line of its own with its state
        in its tone. A step or part an event told HQ of since its record was read says who told it, until
        the next read replaces it.
        """,
        example='{% steps %}{% step icon="check-circle" tone="good" %}Example check holds.{% endstep %}{% endsteps %}',
        classes=("is-numbered", "step-list"),
        partial="partials/_steps.html",
        pieces=("partials/_operator_step.html",),
        about="A list of short steps or checks, each a `{% step %}`, in as many columns as fit.",
        options={
            "numbered": Option(about="Steps done in order: one column, each its number in place of its icon."),
            "of": Option(about="A `sequences.Sequence`, drawn whole, each step numbered with its parts."),
        },
        bare_with="of",
        states={**tags.STEPS_STATES, **sequence.STATES},
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
            "icon": Option(reads=drawn, about="The icon before what it says."),
            "tone": Option(reads=tone_of, about="How the step stands, the color of its icon."),
            "done": Option(about="A finished step, drawn quiet."),
            "title": Option(about="Its tooltip."),
        },
    ),
)
slotted(
    library=register,
    part=Part(
        name="button_row",
        rules="""
        A row of buttons that stands on its own (a confirmation's, a card's decisions, a page's way on) is
        `{% button_row %}`, which a form's closing row draws through too. A sentence beside its buttons
        is `{% muted %}`, and a folded section in it (`{% section ... fold=True %}`) is one of its
        items while closed, opening onto a line of its own the row's whole width.
        """,
        classes=("form-actions",),
        public_classes=("save-bar",),
        partial="partials/_button_row.html",
        about="A row of buttons that stands on its own: a form's closing row, a card's decisions, a page's way on.",
        options={"bar": Option(about="Pinned to the bottom of the screen once its form has changed.")},
        public=True,
        example='{% button_row %}{% button "Save" submit=True look="primary" %}{% endbutton_row %}',
        states=tags.BUTTON_ROW_STATES,
    ),
)
slotted(
    library=register,
    part=Part(
        name="page_main",
        rules="""
        base.html draws every page's content in it, and the component catalog draws each of its entries
        in the same part, so an entry stands where a page's content does. It is the host's frame; an
        extension's page is drawn in it and never writes it.
        """,
        example=tags.PAGE_MAIN,
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
        rules="""
        `{% card css="example" heading="example-heading" %}...{% endcard %}` frames what it encloses. It
        draws a section unless `element=` names an article, figure, details or div. `heading` is its
        heading's id, or `label` its name when it has no heading, and `busy` marks it while its content
        is still coming. `alone` makes it the whole of a page that stands outside the page frame (the
        sign-in): centered at a dialog's width, a picture leading it over its own title, and an action
        alone on its line the card's width. Each `data_*` option is a
        `data-` attribute (True draws it bare). A state of the card is a `data_*` option the stylesheet
        reads, never a class written inside an `{% if %}`. No template writes `class="card"` itself
        (`test_only_the_tag_draws_a_card`). A card is never wider than the column or track it is placed in
        (`.card { min-inline-size: 0 }`), so on a phone what it holds wraps inside it. A card holding only
        a table region (a thing's problems, the dashboard's short queue) has no inset at its sides, so the
        table's rows reach them as a section's do (`:has()` in surfaces.css decides; a page picks
        nothing).

        A band draws no box of its own: a box is the card around it (`{% card element="div" %}`). A card
        holding the band alone is its frame and nothing more: its facts keep their own padding and draw
        their edges as every band does. A card holding nothing but a line of facts is a strip of counts,
        with a narrower inset and its words a size under its values.
        """,
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
        states=tags.CARD_STATES,
    ),
)
