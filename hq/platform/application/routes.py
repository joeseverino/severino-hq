"""Where a named route is, worked out once.

HQ composes its pages from derived structures (the topology, the queue, the
catalogs) whose every node carries the links to its own pages. Composing one
queue asks for several hundred addresses, most of them the same few dozen, and
Django works each out afresh: correct, and a measurable share of the request.

The routes are fixed for the life of the process, so an address worked out once
is the address. ``reverse`` here is Django's, remembered. It is keyed by
everything Django's answer depends on, including the script prefix and the URL
configuration in force, and it forgets everything when a setting changes, which
is how a test that swaps the URL configuration stays correct.

Those two are fixed for one request, and asking Django for them costs more than
the remembered lookup they key, so a projection asks once.

``routed`` is the one walk of the URL configuration, for whatever lists its
routes: a page's name and path, its view, the resolvers it is mounted under.
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from django.core.signals import setting_changed
from django.urls import URLPattern, URLResolver, get_resolver, get_script_prefix, get_urlconf, reverse as _reverse

from .projection import read_once


@lru_cache(maxsize=4096)
def _resolved(urlconf: Any, prefix: str, current_app: Any, viewname: Any, args: tuple, kwargs: tuple) -> str:
    return _reverse(viewname, urlconf=urlconf, args=args or None, kwargs=dict(kwargs) or None, current_app=current_app)


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


@dataclass(frozen=True, slots=True)
class Route:
    """One routed view, as the URL configuration reaches it."""

    pattern: URLPattern
    # The resolvers it is mounted under, outermost first.
    mounts: tuple[URLResolver, ...] = ()

    @property
    def name(self) -> str:
        """``namespace:name`` as ``reverse`` takes it, or "" for an unnamed route."""

        if not self.pattern.name:
            return ""
        return ":".join([*(mount.namespace for mount in self.mounts if mount.namespace), self.pattern.name])

    @property
    def path(self) -> str:
        """The mounts' patterns and its own, joined: ``/control/machines/<str:name>/``."""

        return "/" + "".join(str(mount.pattern) for mount in self.mounts) + str(self.pattern.pattern)

    @property
    def view(self) -> Any:
        """Its class, or a function view itself."""

        return getattr(self.pattern.callback, "view_class", self.pattern.callback)


def routed(
    entries: Iterable[URLPattern | URLResolver] | None = None, mounts: tuple[URLResolver, ...] = ()
) -> Iterator[Route]:
    """Every routed view in the URL configuration in force, in its order."""

    for entry in get_resolver().url_patterns if entries is None else entries:
        if isinstance(entry, URLResolver):
            yield from routed(entry.url_patterns, (*mounts, entry))
        elif isinstance(entry, URLPattern):
            yield Route(entry, mounts)
