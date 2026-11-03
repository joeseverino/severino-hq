"""``{% write_token %}``: what every write form carries in place of ``{% csrf_token %}``.

The CSRF field, and a retry key drawn fresh for this drawing of the form
(``hq.platform.application.web_writes``), so the form sent twice acts once.
"""

from django import template
from django.template.defaulttags import CsrfTokenNode
from django.utils.html import format_html

lazy from hq.platform.application.web_writes import RETRY_KEY_FIELD, new_key

register = template.Library()


@register.simple_tag(takes_context=True)
def write_token(context):
    key = format_html('<input type="hidden" name="{}" value="{}">', RETRY_KEY_FIELD, new_key())
    return CsrfTokenNode().render(context) + key
