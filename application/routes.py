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
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from django.core.signals import setting_changed
from django.urls import get_script_prefix, get_urlconf
from django.urls import reverse as _reverse


@lru_cache(maxsize=4096)
def _resolved(urlconf: Any, prefix: str, current_app: Any, viewname: Any, args: tuple, kwargs: tuple) -> str:
    return _reverse(
        viewname, urlconf=urlconf, args=args or None, kwargs=dict(kwargs) or None, current_app=current_app
    )


def reverse(viewname, urlconf=None, args=None, kwargs=None, current_app=None) -> str:
    """Django's ``reverse``, with the same arguments and the same answer."""

    try:
        return _resolved(
            urlconf if urlconf is not None else get_urlconf(),
            get_script_prefix(),
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
