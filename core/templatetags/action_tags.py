"""Buttons that post on their own, from anywhere on a page.

A page never writes a form for one button. HQ renders one post form, once, at
the end of base.html, and ``post_button`` names it in the button's ``form``
attribute with the button's own ``formaction``. The button therefore works
inside another form without belonging to it: HTML has no nested forms, and a
form written inside another silently detaches every control after it.
"""

from django import template
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

register = template.Library()

POST_FORM_ID = "hq-post"


@register.simple_tag
def post_button(label, url, *, name="action", value="", css="btn", title="", disabled=False):
    attributes = {"name": name, "value": value, "title": title, "aria-label": title}
    return format_html(
        '<button type="submit" form="{}" formaction="{}" class="{}"{}{}>{}</button>',
        POST_FORM_ID,
        url,
        css,
        format_html_join("", ' {}="{}"', ((key, val) for key, val in attributes.items() if val)),
        mark_safe(" disabled") if disabled else "",
        label,
    )
