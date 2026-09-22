from datetime import datetime

from django import template
from django.utils import timezone
from django.utils.formats import date_format

register = template.Library()


@register.filter
def readable(value):
    """An ISO 8601 timestamp as a person reads one; anything else unchanged."""

    if not isinstance(value, str) or "T" not in value:
        return value
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    if timezone.is_aware(moment):
        moment = timezone.localtime(moment)
    return date_format(moment, "M j, Y, g:i a")
