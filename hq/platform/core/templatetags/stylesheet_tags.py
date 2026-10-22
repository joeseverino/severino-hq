"""The stylesheet's links, for a page's head."""

from django import template
from django.templatetags.static import static
from django.utils.html import format_html_join

from hq.platform.core.stylesheets import SHEETS

register = template.Library()


@register.simple_tag
def stylesheets() -> str:
    """One link per sheet, in cascade order."""

    return format_html_join("\n", '<link rel="stylesheet" href="{}">', ((static(sheet),) for sheet in SHEETS))
