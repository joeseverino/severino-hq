"""The control label: the small muted name over or beside a control.

``{% control_label "From" control="whatif-source" %}`` draws
``partials/_control_label.html``: a ``<label>`` for the control whose id
``control`` names, as a filter bar's selects take one, or with no control the
same words naming what follows them.
"""

from django import template

from hq.platform.core.part_examples import tags
from hq.platform.core.template_parts import Part, slotted

register = template.Library()


def control_label(words: str, control: str = "") -> dict[str, str]:
    """A control's name: its words, and the id of the control it labels."""

    return {"words": words, "control": control}


slotted(
    Part(
        name="control_label",
        rules="""
        A control's visible name is `{% control_label "From" control="id" %}`, the `<label>` of the
        control with that id; with no `control` it names what follows.
        """,
        example='{% control_label "From" control="example-from" %}<select id="example-from"><option>example-host</option></select>',
        about="The small quiet name over or beside a control, a `<label>` for the control `control` names.",
        partial="partials/_control_label.html",
        function=control_label,
        classes=("control-label",),
        states=tags.CONTROL_LABEL_STATES,
    ),
    library=register,
)
