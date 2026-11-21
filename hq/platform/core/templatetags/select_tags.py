"""The select: a choice of one, which may say what is chosen in a status tone."""

from django import template

from hq.platform.application.tone import Tone
from hq.platform.core.part_examples import tags
from hq.platform.core.template_parts import Part, slotted

register = template.Library()

SELECT_PARTIAL = "partials/_select.html"


def select(name, options, chosen=None, label="", id="", tone="", every="", submit_on_change=False) -> dict:
    """A choice of one: its field's name, each option with whether it is the
    chosen one, and the tone's classes."""

    try:
        tone = Tone(tone) if tone else ""
    except ValueError:
        raise template.TemplateSyntaxError(f"select tone= is one of {', '.join(Tone)}") from None
    if not name:
        raise template.TemplateSyntaxError("select takes the name its form posts the choice as")
    # None chooses the option with no value, as a field left at its default does.
    chosen = str(chosen or "")
    # A value alone is its own words: a year, a port, a machine's name.
    options = tuple(option if isinstance(option, (tuple, list)) else (option, option) for option in options or ())
    if every:
        # A choice with no value of its own is a view's or an order's
        # default, named for what it shows, which "Any" would contradict.
        if any(not value for value, _ in options):
            raise template.TemplateSyntaxError(f"select every= narrows; {name} names its own default, so it picks")
        options = (("", f"Any {every}"), *options)
    return {
        "name": name,
        "id": id,
        "label": label,
        "choices": [(str(value or ""), words, str(value or "") == chosen) for value, words in options],
        "classes": f"toned tone-{tone}" if tone else "",
        "submit_on_change": submit_on_change,
    }


slotted(
    Part(
        name="select",
        about="A choice of one, which may say what is chosen in a status tone.",
        rules="""
        A choice of one drawn from data is `{% select "rule" choices chosen=rule label="What it sets"
        tone=tone %}`: a `<select>` named by its first argument, an option for each `(value, words)` of
        its second, the chosen one selected, and `label` the name a reader hears (a table cell's
        setting). With an `id` its `label` is shown, the `{% control_label %}` drawn before it for that
        id, as a question's fields are named (From, To, Port). A value alone is its
        own words (a year, a port), and `submit_on_change=True` sends the form as the choice changes,
        for a bar whose one choice is its whole query. `tone` says the choice in a status tone, the
        status part's ink, edged in it unless neutral, as an agent's rule reads "On" in the good tone and
        "Off" in the serious one. A select that narrows names the dimension it narrows (`every="type"`),
        and the part leads it with "Any type", which claims no total, so no summary contradicts it. A
        select that picks a view or an order (the map's View, a table's Sort) offers only named choices.
        Its default is named for what it shows ("Main lanes"), never Any or All. The empty option comes
        only from `every=`.
        """,
        partial=SELECT_PARTIAL,
        function=select,
        example=tags.SELECT,
        states=tags.SELECT_STATES,
    ),
    library=register,
)
