"""The stylesheet's links, for a page's head."""

from pathlib import PurePath

from django import template
from django.templatetags.static import static
from django.utils.html import format_html, format_html_join

from hq.platform.core.stylesheets import FACES, SHEETS

register = template.Library()


@register.simple_tag
def stylesheets() -> str:
    """A preload per face the sheets declare, then one link per sheet, in cascade order."""

    return format_html(
        "{}\n{}",
        format_html_join(
            "\n",
            '<link rel="preload" href="{}" as="font" type="font/{}" crossorigin>',
            ((static(face), PurePath(face).suffix[1:]) for face in FACES),
        ),
        format_html_join("\n", '<link rel="stylesheet" href="{}">', ((static(sheet),) for sheet in SHEETS)),
    )
