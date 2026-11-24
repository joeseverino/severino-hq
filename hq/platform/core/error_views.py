"""The pages HQ shows when it fails or refuses, built from nothing that may have failed."""

import logging

from django.http import HttpResponseForbidden, HttpResponseServerError
from django.template import loader
from django.template.context_processors import csp
from django.urls import resolve, reverse

from hq.platform.application.pages import principal_opens
from hq.platform.application.request_identity import signed_in
from hq.platform.application.security import web_principal

from .context_processors import site

logger = logging.getLogger(__name__)


def server_error(request):
    """Django's 500 skips the context processors, rightly: any of them may be
    what broke. HQ's name and asset version come from settings alone, and the
    script nonce from the policy middleware, so those two are given to the page
    and the rest are not. The request id is the one the log line for this
    failure carries, and the error HQ keeps under it (``server_errors``) is
    linked for a reader who may open its page."""

    request_id = getattr(request, "request_id", "")
    context = {**site(request), **csp(request), "request_id": request_id, "error_url": _error_url(request, request_id)}
    return HttpResponseServerError(loader.get_template("500.html").render(context))


def _error_url(request, request_id: str) -> str:
    """The page of the error kept under ``request_id``, for a signed-in reader who may open it; else ""."""

    if not request_id:
        return ""
    try:
        if (user := signed_in(request)) is None:
            return ""
        url = reverse("errors:detail", args=[request_id])
        return url if principal_opens(web_principal(user), resolve(url).func) else ""
    except Exception as exc:  # noqa: BLE001 - the page answers whatever else has failed
        logger.warning("The error page could not link its error: %s", exc.__class__.__name__)
        return ""


def csrf_failure(request, reason=""):
    """A form posted without a valid token: almost always a page left open
    past its session. The same page as any other refusal, saying so; the
    reason Django gives is for the log and is not shown."""

    return HttpResponseForbidden(loader.get_template("403.html").render({"stale": True}, request))
