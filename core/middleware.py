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
        token = request_logging.set_request_id(request_id)
        started = monotonic()
        try:
            response = self.get_response(request)
            response["X-Request-ID"] = request_id
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
        path = request.path
        for prefix in settings.LOGIN_EXEMPT_PATH_PREFIXES:
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
