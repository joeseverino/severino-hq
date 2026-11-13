"""The tags a write form is drawn through.

``{% write_token %}``: what every write form carries in place of
``{% csrf_token %}``: the CSRF field, and a retry key drawn fresh for this
drawing of the form (``hq.platform.application.web_writes``), so the form sent
twice acts once.

``{% row_editor %}``: a form that edits one row of a table, opened under that
row (docs/Design.md, A row's editor opens under it).

``form|asks_one_question``: whether a form is one line of words to fill, which
partials/_form.html draws on its button's line (docs/Design.md, A form that
asks one question).
"""

import copy

from django import template
from django.forms.widgets import Input, Select
from django.template.defaulttags import CsrfTokenNode
from django.utils.html import format_html
from django.utils.text import slugify

lazy from hq.platform.application.web_writes import RETRY_KEY_FIELD, new_key

register = template.Library()

# The query parameter that names the row whose editor is open.
ROW_EDIT = "edit"


@register.simple_tag(takes_context=True)
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


@register.inclusion_tag("partials/_row_editor.html", takes_context=True)
def row_editor(context, form, key, *, label, action, verb, **initial):
    """The way into a row's editor, and the editor itself where the page's
    address opens it (``?edit=<key>``).

    ``form`` is the one form the view declares for the table's editors, drawn
    for the open row alone: with that row's values (``initial``, by field
    name) unless it is bound, when it is drawn as it was sent. Its ids carry
    the row's ``key``, so they are the row's own. ``label`` names the way in,
    ``action`` is where the editor posts and ``verb`` its button's words.
    """

    unknown = sorted(set(initial) - set(form.fields))
    if unknown:
        raise template.TemplateSyntaxError(f"row_editor sets only the form's own fields, not {', '.join(unknown)}.")
    request = context["request"]
    slug = slugify(str(key))
    asked = request.GET.copy()
    is_open = asked.get(ROW_EDIT) == str(key)
    asked.pop(ROW_EDIT, None)
    close_url = f"{request.path}?{asked.urlencode()}" if asked else request.path
    asked[ROW_EDIT] = str(key)
    drawn = None
    if is_open:
        drawn = copy.copy(form)
        drawn.auto_id = f"row-{slug}-%s"
        # A copy shares the bound fields its form already drew, with their ids
        # and values; Django offers no public way to draw them afresh.
        drawn._bound_fields_cache = {}  # noqa: SLF001 (starts the copy's fields over)
        drawn.initial = {**form.initial, **initial}
    return {
        "label": label,
        "slug": slug,
        "open": is_open,
        "open_url": f"{request.path}?{asked.urlencode()}#row-{slug}",
        "close_url": close_url,
        "form": drawn,
        "action": action,
        "verb": verb,
    }
