"""The pages HQ shows when it fails or refuses, built from nothing that may have failed."""

from django.http import HttpResponseForbidden, HttpResponseServerError
from django.template import loader

from .context_processors import site


def server_error(request):
    """Django's 500 skips the context processors, rightly: any of them may be
    what broke. HQ's name and asset version come from settings alone, so this
    one is given to the page and the rest are not. The request id is the one
    the log line for this failure carries."""

    context = {**site(request), "request_id": getattr(request, "request_id", "")}
    return HttpResponseServerError(loader.get_template("500.html").render(context))


def csrf_failure(request, reason=""):
    """A form posted without a valid token: almost always a page left open
    past its session. The same page as any other refusal, saying so; the
    reason Django gives is for the log and is not shown."""

    return HttpResponseForbidden(loader.get_template("403.html").render({"stale": True}, request))
