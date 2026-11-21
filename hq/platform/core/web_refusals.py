"""A refused answer as a person reads it, on a page or as the whole response."""

from typing import Any

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
lazy from django.forms import BaseForm

from hq.platform.application.refusal_status import Refusal, refusal_of
from hq.platform.application.security import Principal, web_principal
lazy from hq.platform.application.automation_runs import held_words
lazy from hq.platform.core.held_values import format_html
lazy from hq.platform.core.templatetags.action_tags import offer_markup
lazy from hq.platform.core.templatetags.value_tags import breakable


def refused(request: HttpRequest, answer: dict[str, Any], *, form: BaseForm | None = None) -> Refusal | None:
    """The refusal ``answer`` carries, or None when it ran, said on the form drawn again
    (each field's detail beside it, else the sentence above) or on the next page.
    Its ``status`` is the one the view answers with."""

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
    """The refusal's sentence as a page says it: a run in the way is named and linked,
    with its cancel where the reader may, where the API points at ``details.holding``."""

    held = held_words(refusal.details, principal)
    if held is None:
        return refusal.message
    said = format_html('{} <a href="{}">{}</a>', breakable(held.sentence), held.href, breakable(held.link))
    if held.cancel is None:
        return said
    return format_html("{} {}", said, offer_markup(held.cancel, look=f"{held.cancel.look} compact"))


def refusal_response(answer: dict[str, Any]) -> HttpResponse:
    """A refused answer as the whole response, for a post no page is drawn for: its sentence, at its status."""

    refusal = refusal_of(answer)
    return HttpResponse(refusal.message, status=refusal.status, content_type="text/plain; charset=utf-8")


def _placed(form: BaseForm, details: Any) -> bool:
    """Put each field's problem from an ``invalid_input`` refusal's details (the list ``input_errors``
    describes, its location the field) beside it; whether all were. A problem
    inside a field (one coordinate of a point) is left to the sentence, which
    names the part."""

    if not isinstance(details, list) or not details:
        return False
    placed = True
    for item in details:
        location = item.get("loc") if isinstance(item, dict) else None
        field = str(location[0]) if location and len(location) == 1 else ""
        if field in form.fields and item.get("msg"):
            form.add_error(field, item["msg"])
        else:
            placed = False
    return placed
