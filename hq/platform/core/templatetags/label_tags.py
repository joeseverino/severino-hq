"""The control label: the small muted name over or beside a control.

``{% control_label "From" control="whatif-source" %}`` draws
``partials/_control_label.html``: a ``<label>`` for the control whose id
``control`` names, as a filter bar's selects take one, or with no control the
same words naming what follows them.
"""

from django import template

register = template.Library()


@register.inclusion_tag("partials/_control_label.html")
def control_label(words: str, control: str = "") -> dict[str, str]:
    """A control's name: its words, and the id of the control it labels."""

    return {"words": words, "control": control}
