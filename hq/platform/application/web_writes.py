"""A browser's write: the retry key its form posts, sent with the capability it runs.

Every write form carries ``{% write_token %}``, which draws the CSRF field and
a fresh key, and a view runs its capabilities through ``execute_posted`` or
``run_posted``, which send that key. So a form sent twice (a double press, a
resend after an answer that never arrived, a fragment posting again) acts
once, as a token's write does. ``fragment.js``'s ``hqFetch`` draws the form a
new key once a post is answered, so a form that stays on the page asks afresh.

A form view sends its form's values as the payload ``form_payload`` makes
of them, and answers a resend from ``posted_answer`` before its form is
validated again. Only a published form mixin whose write is no capability
(``writes.outside_the_runner``) runs it through ``write_once`` instead.

A refused answer reaches the person through ``refused``, said on the form or
the next page, or as the whole response through ``refusal_response``.
"""

import json
import secrets
from collections.abc import Callable, Iterable
from dataclasses import fields
from typing import Any

from django.contrib import messages
from django.core.exceptions import ImproperlyConfigured
from django.core.files.uploadedfile import UploadedFile
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Model
from django.http import HttpRequest, HttpResponse
lazy from django.forms import BaseForm
lazy from django.utils.html import format_html

lazy from hq.platform.core.templatetags.action_tags import post_button
lazy from hq.platform.core.templatetags.value_tags import breakable

from .capabilities import Ran, run_capability
from .forms import file_as_sent
from .idempotency import (
    KEY,
    IdempotencyConflict,
    InvalidIdempotencyKey,
    answered,
    execute_form_once,
    validate_key,
)
from .refusal_status import Refusal, refusal_of, refuse
from .security import Principal, agent_subject, web_principal
lazy from .automation_runs import held_words
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
    """Run ``name`` as the signed-in person, under the key their form posted."""

    return run_capability(
        name,
        payload,
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
    """``run_posted``'s answer alone, as ``execute_capability`` is ``run_capability``'s."""

    return run_posted(request, name, payload, target=target, expected_updated_at=expected_updated_at).answer


def refused(request: HttpRequest, answer: dict[str, Any], *, form: BaseForm | None = None) -> Refusal | None:
    """The refusal ``answer`` carries, said where the person will read it, or None when it ran.

    On a form drawn again, each detail that names one of its fields goes beside
    that field, and the refusal's own sentence above the form unless the
    details placed all of it. Anywhere else the sentence goes on the next
    page's messages. Its ``status`` is the one the view answers with.
    """

    if answer.get("ok"):
        return None
    refusal = refusal_of(answer)
    # Only a signed-in reader is offered a cancel of the run in the way.
    user = request.user
    said = _said(refusal, web_principal(user) if user.is_authenticated else None)
    if form is None:
        messages.error(request, said)
    elif not _placed(form, refusal.details):
        form.add_error(None, said)
    return refusal


def _said(refusal: Refusal, principal: Principal | None) -> str:
    """The refusal's sentence as a page says it: a run refused for another in
    its way names that run, links to it and offers to cancel it where the
    reader may, where the machine API's sentence points at ``details.holding``."""

    held = held_words(refusal.details, principal)
    if held is None:
        return refusal.message
    said = format_html('{} <a href="{}">{}</a>', breakable(held.sentence), held.href, breakable(held.link))
    if held.cancel is None:
        return said
    cancel = held.cancel
    return format_html(
        "{} {}",
        said,
        post_button(cancel.label, cancel.url, name=cancel.field, value=cancel.value, css=f"{cancel.css} compact"),
    )


def refusal_response(answer: dict[str, Any]) -> HttpResponse:
    """A refused answer as the whole response, for a post no page is drawn for: its sentence, at its status."""

    refusal = refusal_of(answer)
    return HttpResponse(refusal.message, status=refusal.status, content_type="text/plain; charset=utf-8")


def _placed(form: BaseForm, details: Any) -> bool:
    """Put each field's problem from an ``invalid_input`` refusal's details beside it; whether all were.

    The details are the Pydantic list ``input_errors`` describes: a location
    whose first part is the field, and a message that names no value sent.
    """

    if not isinstance(details, list) or not details:
        return False
    placed = True
    for item in details:
        location = item.get("loc") if isinstance(item, dict) else None
        field = str(location[0]) if location else ""
        if field in form.fields and item.get("msg"):
            form.add_error(field, item["msg"])
        else:
            placed = False
    return placed


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


def write_once(request: HttpRequest, write: str, operation: Callable[[], dict[str, Any]]) -> Ran:
    """Run a form view's own write, named ``write``, once under the key its form posted.

    ``operation`` writes and answers what a resend is answered with: JSON
    values naming what it wrote, never a model, since a replay has nothing of
    the first request but its answer. It raises to refuse, which frees the key. A post
    without a key is refused as a capability refuses the browser one.
    """

    key = _call_key(request)
    if key is None:
        return Ran(refuse("idempotency_key_required", "This form was sent without its retry key: reload the page."))
    try:
        answer, replayed = execute_form_once(
            actor=agent_subject(web_principal(request.user)),
            key=validate_key(key),
            write=write,
            form=_posted_form(request),
            operation=lambda: {**operation(), "ok": True},
        )
    except InvalidIdempotencyKey as exc:
        return Ran(refuse("invalid_idempotency_key", exc.reason))
    except IdempotencyConflict as exc:
        return Ran(refuse("idempotency_conflict", exc.reason))
    return Ran(answer, replayed)


def posted_answer(request: HttpRequest) -> dict[str, Any] | None:
    """What this same post was answered the first time it wrote, or None.

    Read before the form is validated again: what the first post wrote can
    make its own resend invalid (a slug now taken, a record now gone). An
    answer found stands for the post's first call, so each call the view makes
    after it takes the key it took the first time.
    """

    sent = request.POST.get(RETRY_KEY_FIELD) or None
    if sent is None or not KEY.fullmatch(sent):
        return None
    answer = answered(actor=agent_subject(web_principal(request.user)), key=sent)
    if answer is not None:
        _call_key(request)
    return answer


# What a post carries besides its form: the CSRF field changes with the
# session, and the key is what names the request rather than part of it.
_NOT_THE_REQUEST = frozenset({RETRY_KEY_FIELD, "csrfmiddlewaretoken"})


def _posted_form(request: HttpRequest) -> dict[str, Any]:
    """The request a form's key names: where it posted, every field, and each file by name and size."""

    return {
        "path": request.get_full_path(),
        "fields": sorted((name, values) for name, values in request.POST.lists() if name not in _NOT_THE_REQUEST),
        "files": sorted(
            (name, [(upload.name, upload.size) for upload in uploads]) for name, uploads in request.FILES.lists()
        ),
    }


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
    sent = {name: _as_sent(value) for name, value in form.cleaned_data.items() if name in takes}
    return json.loads(json.dumps(sent, cls=DjangoJSONEncoder))


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
