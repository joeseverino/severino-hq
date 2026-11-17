"""The select: a choice of one, which may say what is chosen in a status tone.

``{% select "rule" choices chosen=rule label="Edit a record, for example-agent" tone="good" %}``
draws ``partials/_select.html``: a ``<select>`` named by its first argument
with an option for each ``(value, words)`` of its second, or each value that
is its own words, the one whose value is ``chosen`` selected. ``id`` is the one a ``{% control_label %}`` names it
by; with no visible label, ``label`` is the name a reader hears (a table
cell, whose row and column say what it sets). ``tone`` draws it in a status
tone, the status part's ``toned`` ink, as a rule that allows reads "good".
``every`` names the dimension a filter bar's choice narrows (``every="type"``),
and leads with the option that narrows nothing, "Any type" (docs/Design.md,
"One filter bar"). ``submit_on_change`` sends its form as soon as the choice
changes, for a bar whose one choice is the whole query.
"""

from django import template

from hq.platform.application.tone import Tone

register = template.Library()

SELECT_PARTIAL = "partials/_select.html"


@register.inclusion_tag(SELECT_PARTIAL)
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
        "classes": f"toned toned-{tone}" if tone else "",
        "submit_on_change": submit_on_change,
    }
