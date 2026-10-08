"""Where a named route is, worked out once.

HQ composes its pages from derived structures (the topology, the queue, the
catalogues) whose every node carries the links to its own pages. Composing one
queue asks for several hundred addresses, most of them the same few dozen, and
Django works each out afresh: correct, and a measurable share of the request.

The routes are fixed for the life of the process, so an address worked out once
is the address. ``reverse`` here is Django's, remembered. It is keyed by
everything Django's answer depends on, including the script prefix and the URL
configuration in force, and it forgets everything when a setting changes, which
is how a test that swaps the URL configuration stays correct.

Those two are fixed for one request, and asking Django for them costs more than
the remembered lookup they key, so a projection asks once.
"""

from functools import lru_cache
from typing import Any

from django.core.signals import setting_changed
from django.urls import get_script_prefix, get_urlconf, reverse as _reverse

from .projection import read_once


@lru_cache(maxsize=4096)
def _resolved(urlconf: Any, prefix: str, current_app: Any, viewname: Any, args: tuple, kwargs: tuple) -> str:
    return _reverse(
        viewname, urlconf=urlconf, args=args or None, kwargs=dict(kwargs) or None, current_app=current_app
    )


def _in_force() -> tuple[Any, str]:
    """The URL configuration and script prefix this projection is served under."""

    return read_once("routes.in_force", lambda: (get_urlconf(), get_script_prefix()))


def reverse(viewname, urlconf=None, args=None, kwargs=None, current_app=None) -> str:
    """Django's ``reverse``, with the same arguments and the same answer."""

    serving, prefix = _in_force()
    try:
        return _resolved(
            urlconf if urlconf is not None else serving,
            prefix,
            current_app,
            viewname,
            tuple(args or ()),
            tuple(sorted((kwargs or {}).items())),
        )
    except TypeError:
        # Something unhashable was passed (a view callable, a list argument):
        # asked of Django directly, as before.
        return _reverse(viewname, urlconf=urlconf, args=args, kwargs=kwargs, current_app=current_app)


def _forget(**_kwargs) -> None:
    _resolved.cache_clear()


setting_changed.connect(_forget)
