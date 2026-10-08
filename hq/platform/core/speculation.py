"""A page the browser fetches before it is asked for, and what such a fetch may not do.

Every page names one set of speculation rules in a ``Speculation-Rules``
header: prefetch a same-origin link when the pointer goes down on it, so the
document is already on its way when the click completes. Prefetch only: a
prefetched document runs no script. A request to prerender is refused whole.

A speculative request is a guess, so it has no effect. That is held where HQ
already knows an effect happens, not by a list of addresses:

- a route that answers without a session (signing in and out, the identity
  provider's handshake, probes, token APIs) is refused before its view runs;
- the audit writer refuses, so anything audited is not done;
- the outbound boundary refuses, so nothing outside the process is asked;
- an answer that sends the browser to another origin is withheld.

Each is answered 503, which a browser discards and then navigates normally
when the link is followed. ``SpeculativeRequestTests`` requests every page
speculatively and fails on one that writes without being refused.
"""

import json
import re
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from functools import cache
from typing import Any
from urllib.parse import urlsplit

from django.http import HttpRequest, HttpResponse
from django.urls import URLPattern, URLResolver, get_resolver, reverse
from django.urls.resolvers import RoutePattern
from django.utils.cache import patch_vary_headers
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_safe

RULES_TYPE = "application/speculationrules+json"

# Links that are not a page to arrive at: a file, another window, a dialog, a
# region or a table answered in place.
NOT_A_NAVIGATION = (
    "a[download], a[target], a[data-modal-open], a[data-fragment-target], "
    "[data-fragment-links] a, a.table-sort-link, .pagination a"
)

# The reasons the request being served was refused, when it is speculative.
_refused: ContextVar[list[str] | None] = ContextVar("speculation_refused", default=None)


class Speculative(RuntimeError):
    """A speculative request reached something that has an effect."""


def purpose(request: HttpRequest) -> str:
    """ "prefetch", "prerender", or "" for a request somebody actually made."""

    sent = request.headers.get("Sec-Purpose", "").lower()
    if "prerender" in sent:
        return "prerender"
    if sent.startswith("prefetch") or request.headers.get("Purpose", "").lower() == "prefetch":
        return "prefetch"
    return ""


def refuse(reason: str) -> None:
    """Called where an effect is about to happen: a speculative request stops here.

    Nothing when the request being served is one somebody made. The refusal is
    recorded as well as raised, so a caller that catches the exception still
    cannot turn the request into an answer.
    """

    reasons = _refused.get()
    if reasons is None:
        return
    reasons.append(reason)
    raise Speculative(f"A speculative request has no effect: {reason}.")


def _withheld(reason: str) -> HttpResponse:
    response = HttpResponse(status=503)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Speculation-Refused"] = reason
    return response


def _routes(patterns: Any = None, prefix: str = "") -> Iterator[tuple[str, URLPattern]]:
    for pattern in patterns if patterns is not None else get_resolver().url_patterns:
        if isinstance(pattern, URLResolver):
            mount = pattern.pattern
            here = str(mount) if isinstance(mount, RoutePattern) else ""
            yield from _routes(pattern.url_patterns, prefix + here)
        else:
            yield prefix, pattern


@cache
def sessionless() -> tuple[str, ...]:
    """Every route that answers without a session, as a URL pattern.

    Read from the URL configuration: the routes marked ``login_not_required``
    and the prefixes an extension authenticates itself.
    """

    from hq.platform.application.plugins import plugin_token_authenticated_prefixes

    found = {f"{prefix}*" for prefix in plugin_token_authenticated_prefixes()}
    for prefix, pattern in _routes():
        if getattr(pattern.callback, "login_required", True):
            continue
        route = str(pattern.pattern) if isinstance(pattern.pattern, RoutePattern) else "*"
        found.add("/" + re.sub(r"<[^>]+>", "*", prefix + route))
    return tuple(sorted(found))


def rules() -> dict[str, Any]:
    """The one rule set: prefetch a page of HQ's when its link is pressed.

    ``conservative`` is the pointer or the finger going down. A page is then
    fetched while the click completes and shown at once, and never sits
    fetched and unread while the operator changes what it would show.
    """

    return {
        "prefetch": [
            {
                "where": {
                    "and": [
                        {"href_matches": "/*", "relative_to": "document"},
                        {"not": {"href_matches": list(sessionless()), "relative_to": "document"}},
                        {"not": {"selector_matches": NOT_A_NAVIGATION}},
                    ]
                },
                "eagerness": "conservative",
            }
        ]
    }


@require_safe
@cache_control(private=True, max_age=3600)
def rules_document(request: HttpRequest) -> HttpResponse:
    """The rules, as the document the header points at.

    Behind the session like any page: it names the routes an installation
    mounts, and the browser fetches it same-origin, with the session's cookie.
    """

    return HttpResponse(json.dumps(rules(), separators=(",", ":")), content_type=RULES_TYPE)


def answer(request: HttpRequest, get_response: Callable[[HttpRequest], HttpResponse]) -> HttpResponse:
    """``get_response(request)``, held to having no effect when the request is a guess.

    Called by `RequestContextMiddleware`, around authentication and everything
    after it, so the hold applies whichever of them answers.
    """

    asked = purpose(request)
    if asked == "prerender":
        return _withheld("prerender")
    if not asked:
        return _point(request, get_response(request))
    reasons: list[str] = []
    token = _refused.set(reasons)
    try:
        response = get_response(request)
    finally:
        _refused.reset(token)
    if reasons:
        return _withheld(reasons[0])
    target = urlsplit(response.headers.get("Location", ""))
    if target.netloc and target.netloc != request.get_host():
        return _withheld("leaves this origin")
    # One address, two answers: a cache must not hand one out as the other.
    patch_vary_headers(response, ("Sec-Purpose",))
    return response


def before_view(request: HttpRequest, view_func: Any) -> HttpResponse | None:
    """Refuse a speculative request for a route that answers without a session."""

    if _refused.get() is None:
        return None
    from hq.platform.application.plugins import plugin_token_authenticated_prefixes

    own = request.path.startswith(plugin_token_authenticated_prefixes())
    if own or not getattr(view_func, "login_required", True):
        return _withheld("answers without a session")
    return None


def on_exception(exception: Exception) -> HttpResponse | None:
    """A refusal that reached the top of a view is the answer, not an error."""

    return _withheld("has an effect") if isinstance(exception, Speculative) else None


def _point(request: HttpRequest, response: HttpResponse) -> HttpResponse:
    """Name the rules on a page a signed-in person loaded."""

    if (
        request.method == "GET"
        and response.status_code == 200
        and response.headers.get("Content-Type", "").startswith("text/html")
        and getattr(getattr(request, "user", None), "is_authenticated", False)
        and "X-Requested-With" not in request.headers
    ):
        response.headers["Speculation-Rules"] = f'"{reverse("speculation_rules")}"'
    return response
