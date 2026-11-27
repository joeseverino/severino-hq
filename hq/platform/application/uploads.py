"""The largest request HQ takes, derived from every file it is offered.

Each form's file field declares the largest file it takes (``forms.FileField``)
and each command's file its own (``command_fields.Upload``), which the form
drawn from that command takes and a call of it sends as base64, so what HQ
takes is whatever is composed, extensions' forms included. The API and MCP
hold each call's body to it, and the proxy in front of HQ to the largest
(``proxy_findings``).
"""

import math
from functools import cache
from typing import Any, get_args, get_origin, get_type_hints

from django import forms
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.urls import URLResolver, get_resolver, get_urlconf
lazy from annotated_types import MaxLen
lazy from pydantic.fields import FieldInfo

from .command_fields import Upload, upload_of
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


def _carried(command: type, name: str, hint: Any) -> tuple[Upload, int] | None:
    """The ``Upload`` a field's files carry and how many one call sends: one,
    or as many as its list holds."""

    if upload := upload_of(hint):
        return upload, 1
    field = FieldInfo.from_annotation(hint)
    if get_origin(field.annotation) not in {list, tuple} or not (upload := upload_of(get_args(field.annotation)[0])):
        return None
    most = [bound.max_length for bound in field.metadata if isinstance(bound, MaxLen)]
    if not most:
        raise ImproperlyConfigured(
            f"{command.__module__}.{command.__qualname__}.{name} takes any number of files. "
            "Give it Field(max_length=...), which says how many one call sends."
        )
    return upload, min(most)


def _uploads(command: type) -> tuple[tuple[Upload, int], ...]:
    hints = get_type_hints(command, include_extras=True)
    return tuple(found for name, hint in hints.items() if (found := _carried(command, name, hint)))


def _drawn(command: type) -> int:
    """The most bytes of files one post of ``command``'s form sends."""

    return sum(upload.max_bytes * count for upload, count in _uploads(command))


# Read once per command, since its declaration is fixed when HQ starts.
@cache
def _encoded(command: type) -> int:
    """The most bytes of files one call of ``command`` sends as JSON, each base64-encoded."""

    return sum(upload.max_encoded * count for upload, count in _uploads(command))


def call_body(name: str) -> int:
    """The most bytes the JSON body of one call of capability ``name`` may carry:
    its files as base64, with the framing around them, or the largest body
    Django reads (``DATA_UPLOAD_MAX_MEMORY_SIZE``) where that is larger."""

    spec = held_graph().capabilities.get(name)
    files = _encoded(spec.command_type) if spec else 0
    return max(files + FRAMING, settings.DATA_UPLOAD_MAX_MEMORY_SIZE)


def largest_call() -> int:
    """The most bytes the JSON body of a call of any capability may carry."""

    return max(map(call_body, held_graph().capabilities), default=settings.DATA_UPLOAD_MAX_MEMORY_SIZE)


def largest_request() -> int:
    """The most bytes one request HQ takes may carry: its largest form post,
    with the framing around its files, or its largest call."""

    return max(_largest_post(get_resolver(get_urlconf())) + FRAMING, largest_call())


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
