"""A browser's write: the capability a form runs, sent under the retry key the form posts.

Every write form carries ``{% write_token %}``, which draws the CSRF field and
a fresh key, and ``run_posted`` and ``execute_posted`` send that key, so a form
sent twice (a double press, a resend after an answer that never arrived) acts
once, as a token's write does. A refused answer is
``hq.platform.core.web_refusals``' to say.
"""

import json
import secrets
from collections.abc import Iterable
from dataclasses import fields
from typing import Any

from django.contrib import messages
from django.core.exceptions import ImproperlyConfigured
from django.core.files.uploadedfile import UploadedFile
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Model
from django.http import HttpRequest
lazy from django.forms import BaseForm

from . import capabilities
from .capabilities import Ran, as_documented, first_answer, run_capability
from .forms import file_as_sent
from .integration_specs import command_schema
from .json_schemas import fields as whole_fields
from .security import web_principal
lazy from .records import records_for

# The hidden field the key travels in: a name no command declares, like the
# command form's other fields of its own.
RETRY_KEY_FIELD = "__retry_key"


def new_key() -> str:
    """A key for one drawing of a form."""

    return f"web:{secrets.token_urlsafe(24)}"


def run_posted(
    request: HttpRequest,
    name: str,
    payload: dict[str, Any],
    *,
    target: str | int | None = None,
    expected_updated_at: str | None = None,
) -> Ran:
    """Run ``name`` as the signed-in person, under the key their form posted,
    with what the form sent as text read in the command's types (``as_documented``)."""

    spec = capabilities.capability_spec(name)
    return run_capability(
        name,
        payload if spec is None else as_documented(spec.command_type, payload),
        principal=web_principal(request.user),
        target=target,
        expected_updated_at=expected_updated_at,
        idempotency_key=_call_key(request),
    )


def execute_posted(
    request: HttpRequest,
    name: str,
    payload: dict[str, Any],
    *,
    target: str | int | None = None,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """``run_posted``'s answer alone, its ``message`` said on the next page (``said``)."""

    answer = run_posted(request, name, payload, target=target, expected_updated_at=expected_updated_at).answer
    said(request, answer)
    return answer


def said(request: HttpRequest, answer: dict[str, Any]) -> None:
    """Put what a write that ran says (its answer's ``message``) on the next page."""

    if answer.get("ok") and (message := str(answer.get("message") or "")):
        messages.success(request, message)


def _call_key(request: HttpRequest) -> str | None:
    """The key for this request's next capability call, or None when the form posted none.

    One post may run several capabilities, and a key names one request, so the
    first call takes the posted key and each later one the key with its place
    appended. A resend runs the same calls in the same order, so each meets its
    own first answer.
    """

    sent = request.POST.get(RETRY_KEY_FIELD) or None
    if sent is None:
        return None
    calls = request.__dict__.get("_retry_key_calls", 0) + 1
    request.__dict__["_retry_key_calls"] = calls
    return sent if calls == 1 else f"{sent}.{calls}"


def posted_answer(request: HttpRequest) -> dict[str, Any] | None:
    """What this same post was answered the first time it wrote, or None.

    Read before the form is validated again: what the first post wrote can
    make its own resend invalid (a slug now taken, a record now gone). An
    answer found stands for the post's first call, so each call the view makes
    after it takes the key it took the first time.
    """

    sent = request.POST.get(RETRY_KEY_FIELD) or None
    if sent is None:
        return None
    answer = first_answer(web_principal(request.user), sent)
    if answer is not None:
        _call_key(request)
    return answer


def form_payload(form: BaseForm, command: type) -> dict[str, Any]:
    """A valid form's values as ``command``'s payload, the JSON every door sends.

    A record a field chose is sent by its key, the field its domain names it
    by, and a file picked as ``file_as_sent`` makes it. A field the command
    does not take is left out when it holds what the form was drawn with (a
    stored file not replaced), and refused when it changed, since its value
    would be lost.
    """

    takes = {field.name for field in fields(command)}
    lost = [name for name in form.changed_data if name in form.cleaned_data and name not in takes]
    if lost:
        raise ImproperlyConfigured(f"{command.__name__} takes no {', '.join(lost)}, which {type(form).__name__} sends.")
    # A field left blank that the command cannot hold empty (an amount) is
    # left out, as a caller leaves it out: it takes its default, or an update keeps it.
    nullable = {name for name, field in whole_fields(command_schema(command)).items() if _takes_null(field)}
    sent = {
        name: _as_sent(value)
        for name, value in form.cleaned_data.items()
        if name in takes and (value is not None or name in nullable)
    }
    return json.loads(json.dumps(sent, cls=DjangoJSONEncoder))


def _takes_null(field: dict[str, Any]) -> bool:
    arms = field.get("anyOf", [field])
    return any(arm.get("type") == "null" or not {"type", "$ref", "enum"} & arm.keys() for arm in arms)


def _as_sent(value: Any) -> Any:
    if isinstance(value, Model):
        return _key(value)
    if isinstance(value, UploadedFile):
        return file_as_sent(value)
    if isinstance(value, Iterable) and not isinstance(value, str | bytes | dict):
        return [_as_sent(item) for item in value]
    return value


def _key(record: Model) -> Any:
    """What a command names ``record`` by: its domain's lookup, or its primary key."""

    try:
        lookup = records_for(type(record)).lookup
    except LookupError:
        return record.pk
    return getattr(record, lookup)
