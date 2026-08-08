"""
Middleware for Severino HQ.

- LoginRequiredMiddleware: this is a single-user / internal app; every URL
  requires authentication unless explicitly exempted.
- CurrentUserMiddleware: scopes the request user to the active ASGI context so
  ORM signals can attribute audit events without leaking across requests.
"""

from __future__ import annotations

from contextvars import ContextVar

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.urls import resolve, Resolver404


_current_user = ContextVar("severino_current_user", default=None)


def get_current_user():
    return _current_user.get()


def set_current_user(user) -> None:
    _current_user.set(user)


class CurrentUserMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _current_user.set(getattr(request, "user", None))
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
