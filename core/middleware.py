"""
Middleware for Severino HQ.

- LoginRequiredMiddleware: this is a single-user / internal app; every URL
  requires authentication unless explicitly exempted.
- CurrentUserMiddleware: scopes the request user to the active ASGI context so
  ORM signals can attribute audit events without leaking across requests.
"""

from __future__ import annotations

from contextvars import ContextVar
import logging
from time import monotonic
from uuid import uuid4

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.urls import resolve, Resolver404

from application.cadence import note_activity

import core.logging as request_logging


_current_user = ContextVar("severino_current_user", default=None)
_request_logger = logging.getLogger("severino.request")


class RequestContextMiddleware:
    """Attach a server-generated correlation ID and one bounded access log."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = uuid4().hex
        request.request_id = request_id
        # How often the controller sweeps depends on whether anybody is here.
        # A stat on most requests and a small write on the first of each
        # interval; see `application.cadence`.
        note_activity()
        token = request_logging.set_request_id(request_id)
        started = monotonic()
        try:
            response = self.get_response(request)
            response["X-Request-ID"] = request_id
            # Django has settings for the other browser-boundary headers but
            # not these three. HQ uses none of these APIs, and an operator
            # console holding provider credentials has no reason to leave them
            # available to anything that manages to run in the page.
            response.setdefault(
                "Permissions-Policy",
                "geolocation=(), microphone=(), camera=(), usb=(), payment=(), "
                "interest-cohort=()",
            )
            # Nothing here is meant to be read by another origin. Django's
            # default opener policy already isolates the browsing context;
            # this is the other half -- another site cannot pull a page, an
            # export or a receipt into its own document as a subresource, so a
            # cross-origin read cannot be laundered through an <img> or a
            # <script> tag and measured.
            response.setdefault("Cross-Origin-Resource-Policy", "same-origin")
            # Where `report-to` in the policy resolves to. Named `csp` because
            # that is the group the policy references; the endpoint is on this
            # origin, so a report never leaves the tailnet.
            response.setdefault(
                "Reporting-Endpoints",
                f'csp="{settings.SEVERINO_CSP_REPORT_PATH}"',
            )
            if not request.path.startswith("/health/") or response.status_code >= 500:
                _request_logger.info(
                    "request completed",
                    extra={
                        "event": "http.request",
                        "method": request.method,
                        "path": request.path,
                        "status": response.status_code,
                        "duration_ms": round((monotonic() - started) * 1000, 2),
                    },
                )
            return response
        finally:
            request_logging.reset_request_id(token)


class AdminPolicyMiddleware:
    """Run Django admin under the one directive its own JavaScript cannot meet.

    The application policy requires Trusted Types, which makes assigning a
    string to `innerHTML` throw rather than parse. HQ's own scripts never do
    that; admin's bundled jQuery does, on every page it renders. The choice was
    between weakening the policy everywhere for one surface and scoping the
    relaxation to that surface, and this is the second.

    A middleware rather than a decorator because the admin routes a view per
    registered model and generates most of them -- a decorator that has to be
    remembered on each is one that will eventually be missed, silently, and the
    admin page that missed it simply stops working.

    Ordered immediately after Django's CSP middleware, which is what makes this
    work at all: response middleware runs outermost-last, so the override has
    to be attached by something *inside* the middleware that reads it.
    """

    # Where the admin is mounted. A test asserts this against the URLconf
    # rather than trusting the two to stay in step: a prefix that stops
    # matching does not fail, it silently serves the admin a policy its own
    # scripts cannot satisfy, and the page is blank.
    prefix = "/admin/"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith(self.prefix):
            response._csp_config = settings.SEVERINO_ADMIN_CSP
        return response


def get_current_user():
    return _current_user.get()


def set_current_user(user) -> None:
    _current_user.set(user)


class CurrentUserMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None:
            # AuthenticationMiddleware exposes a SimpleLazyObject. Keeping it
            # in a ContextVar lets ASGI's context restoration evaluate the
            # session-backed object on the event-loop thread (Django 6.1 then
            # correctly raises SynchronousOnlyOperation). Resolve it while
            # this synchronous middleware is still running in its worker.
            user.is_authenticated
            user = getattr(request, "_cached_user", user)
        token = _current_user.set(user)
        try:
            return self.get_response(request)
        finally:
            _current_user.reset(token)


class LoginRequiredMiddleware:
    """Force authentication on every URL except a small allowlist."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._is_exempt(request):
            return self.get_response(request)

        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)

        return self.get_response(request)

    @staticmethod
    def _is_exempt(request) -> bool:
        from application.plugins import plugin_token_authenticated_prefixes

        path = request.path
        for prefix in settings.LOGIN_EXEMPT_PATH_PREFIXES:
            if path.startswith(prefix):
                return True
        # Routes an extension declared as carrying their own authentication.
        # Skipping the redirect here is not skipping auth: the view still has
        # to authenticate the request, and answers 401 rather than serving an
        # HTML login page to a client that cannot use one.
        for prefix in plugin_token_authenticated_prefixes():
            if path.startswith(prefix):
                return True
        try:
            match = resolve(path)
            if match.url_name in settings.LOGIN_EXEMPT_URL_NAMES:
                return True
        except Resolver404:
            # Unresolvable paths are non-exempt and continue to authentication.
            pass
        return False
