"""The largest request HQ takes, derived from every file it is offered.

Each form's file field declares the largest file it takes (``forms.FileField``)
and each command's file its own (``command_fields.Upload``), which the form
drawn from that command takes, so what HQ takes is whatever is composed,
extensions' forms included, and the proxy in front of HQ is held to it
(``proxy_findings``).
"""

import math
from functools import cache
from typing import get_type_hints

from django import forms
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.urls import URLResolver, get_resolver, get_urlconf

from .command_fields import upload_of
from .forms import FileField
from .integrations import held_graph
from .routes import routed

MIB = 1024 * 1024
# What a post carries beside its files: the multipart framing and the form's
# other fields.
FRAMING = MIB


def _posted(form: type[forms.BaseForm]) -> int:
    """The most bytes of files one post of ``form`` sends."""

    total = 0
    for name, field in form.base_fields.items():
        if not isinstance(field, forms.FileField):
            continue
        if not isinstance(field, FileField):
            raise ImproperlyConfigured(
                f"{form.__module__}.{form.__qualname__}.{name} takes a file of any size. "
                "Make it hq_sdk.forms.FileField, which says the largest it takes."
            )
        total += field.largest_body
    return total


def _drawn(command: type) -> int:
    """The most bytes of files one post of ``command``'s form sends."""

    hints = get_type_hints(command, include_extras=True)
    return sum(upload.max_bytes for hint in hints.values() if (upload := upload_of(hint)))


def largest_request() -> int:
    """The most bytes one request HQ takes may carry: its largest form post,
    with the framing around its files, or the largest other body Django reads
    (``DATA_UPLOAD_MAX_MEMORY_SIZE``), which bounds a command sent as JSON."""

    return max(_largest_post(get_resolver(get_urlconf())) + FRAMING, settings.DATA_UPLOAD_MAX_MEMORY_SIZE)


# Read once per URL configuration in force, the one ``routed`` walks, since
# the composed set is fixed when HQ starts.
@cache
def _largest_post(_urls: URLResolver) -> int:
    views = (route.view for route in routed())
    named = {view.form_class for view in views if isinstance(getattr(view, "form_class", None), type)}
    # A command's form is drawn from its command, so its files are the command's.
    drawn = (_drawn(spec.command_type) for spec in held_graph().capabilities.values())
    return max((*map(_posted, named), *drawn), default=0)


def proxy_body_mib() -> int:
    """The body limit, in MiB, a proxy in front of HQ needs to let every request HQ takes through."""

    return math.ceil(largest_request() / MIB)
