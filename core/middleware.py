"""
Middleware for Severino HQ.

- LoginRequiredMiddleware: this is a single-user / internal app; every URL
  requires authentication unless explicitly exempted.
- CurrentUserMiddleware: stashes the request user on a threadlocal so that
  ORM signals can attribute audit events.
"""

from __future__ import annotations

import threading

from django.conf import settings
from django.shortcuts import redirect
from django.urls import resolve, Resolver404


_thread_locals = threading.local()


def get_current_user():
    return getattr(_thread_locals, "user", None)


def set_current_user(user) -> None:
    _thread_locals.user = user


class CurrentUserMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        set_current_user(getattr(request, "user", None))
        try:
            return self.get_response(request)
        finally:
            set_current_user(None)


class LoginRequiredMiddleware:
    """Force authentication on every URL except a small allowlist."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._is_exempt(request):
            return self.get_response(request)

        if not request.user.is_authenticated:
            login_url = settings.LOGIN_URL
            next_url = request.get_full_path()
            return redirect(f"{login_url}?next={next_url}")

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
            pass
        return False
