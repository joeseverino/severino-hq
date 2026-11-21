"""The tags a write form is drawn through: ``write_token``, ``row_editor`` and
``asks_one_question`` (each Part's rules).

``{% write_token %}`` replaces ``{% csrf_token %}``, adding a retry key fresh
for this drawing of the form, so the form sent twice acts once.
"""

import copy
from typing import Annotated, Any

from django import template
from django.forms.widgets import Input, Select
from django.template.defaulttags import CsrfTokenNode
from django.utils.html import format_html
from django.utils.text import slugify

from hq.platform.application.describe import Describe
from hq.platform.application.url_query import with_query
from hq.platform.core.part_examples import tags
from hq.platform.core.template_parts import Part, slotted
lazy from hq.platform.application.web_writes import RETRY_KEY_FIELD, new_key

register = template.Library()

# The query parameter that names the row whose editor is open.
ROW_EDIT = "edit"


def write_token(context):
    key = format_html('<input type="hidden" name="{}" value="{}">', RETRY_KEY_FIELD, new_key())
    # The CSRF node draws safe markup, or a plain empty string where no token
    # is in context, which would make the sum plain text.
    csrf = CsrfTokenNode().render(context)
    return csrf + key if csrf else key


# A control that takes a line of words or one choice; a box to tick, a set of
# options, a file or anything drawn as several controls takes a form's row.
_NOT_ONE_LINE = frozenset({"checkbox", "file", "hidden"})


@register.filter
def asks_one_question(form):
    """One visible field, and that field one line: a text, number or date
    input, or a single choice."""

    fields = form.visible_fields() if form else ()
    if len(fields) != 1:
        return False
    widget = fields[0].field.widget
    if isinstance(widget, Select):
        return not widget.allow_multiple_selected
    return isinstance(widget, Input) and widget.input_type not in _NOT_ONE_LINE


def row_editor(
    context,
    form: Annotated[Any, Describe("The one form the view declares for the table's editors.")],
    key: Annotated[Any, Describe("The row's key, which its ids and the address that opens it carry.")],
    *,
    label: Annotated[str, Describe("The words of the way in.")],
    action: Annotated[str, Describe("Where the editor posts.")],
    verb: Annotated[str, Describe("Its button's words.")],
    **initial: Annotated[Any, Describe("The row's values, each by its field's name.")],
):
    """The way into a row's editor, and the editor itself where the page's address opens it (``?edit=<key>``).

    ``form`` is drawn for the open row alone, with its ``initial`` values
    unless bound; its ids carry the row's ``key``. ``label``, ``action`` and
    ``verb`` name the way in, where it posts, and its button.
    """

    unknown = sorted(set(initial) - set(form.fields))
    if unknown:
        raise template.TemplateSyntaxError(f"row_editor sets only the form's own fields, not {', '.join(unknown)}.")
    request = context["request"]
    slug = slugify(str(key))
    asked = request.GET.copy()
    is_open = asked.get(ROW_EDIT) == str(key)
    asked.pop(ROW_EDIT, None)
    close_url = with_query(request.path, asked)
    asked[ROW_EDIT] = str(key)
    drawn = None
    if is_open:
        drawn = copy.copy(form)
        drawn.auto_id = f"row-{slug}-%s"
        # A copy shares the bound fields its form already drew, with their ids
        # and values; Django offers no public way to draw them afresh.
        drawn._bound_fields_cache = {}
        drawn.initial = {**form.initial, **initial}
        if form.is_bound:
            # Each field's errors carry the id they were found under, which
            # their list is drawn with and the control's aria-describedby
            # names: the row's own, here.
            drawn._errors = copy.copy(form.errors)
            for name in drawn.fields.keys() & drawn._errors.keys():
                drawn._errors[name] = drawn._errors[name].copy()
                drawn._errors[name].field_id = drawn[name].auto_id
    return {
        "label": label,
        "slug": slug,
        "open": is_open,
        "open_url": with_query(request.path, asked, fragment=f"row-{slug}"),
        "close_url": close_url,
        "form": drawn,
        "action": action,
        "verb": verb,
    }


slotted(
    Part(
        name="write_token",
        about="What every write form carries in place of `{% csrf_token %}`, so the form sent twice acts once.",
        function=write_token,
        public=True,
        example='<form method="post">{% write_token %}...</form>',
    ),
    library=register,
)
slotted(
    Part(
        name="row_editor",
        rules="""
        **A row's editor opens under it.** A row edited on the page holds `{% row_editor form key
        label=... action=... verb=... field=value %}` in a cell: a link to the page with that row's
        editor open (`?edit=<key>`, the other query kept). On that page the region moves the form
        (`.row-editor`) into the line beneath the row and draws the row open, its toggle saying so, and
        with no script that line shows, since no toggle can open it. The view declares one form for the
        table, drawn for the open row alone. It holds the values the cell gives by field name, or what
        was sent when it is bound. Its ids carry the row's key, so none is drawn twice. A region that
        holds an editor's link is a region of the page (`data-fragment`, its id from its label). So with
        script the link, the form and its Cancel are answered there through `fragment.js`'s own paths.
        The form posts `next`, the page with the editor closed. *Gate:*
        `hq/platform/core/tests/test_row_editor.py`.
        """,
        classes=("row-editor",),
        about="The way into a row's editor, and the editor itself under the row where the page's address opens it.",
        partial="partials/_row_editor.html",
        function=row_editor,
        public=True,
        example=tags.ROW_EDITOR,
        states=tags.ROW_EDITOR_STATES,
    ),
    library=register,
)
