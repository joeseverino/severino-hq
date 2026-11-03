"""Authorization and navigation helpers for plugin-owned Django views."""

from collections.abc import Callable
from typing import Concatenate

from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponseBase

from hq.platform.application.security import safe_next

View = Callable[Concatenate[HttpRequest, ...], HttpResponseBase]


def capability_required(capability: str) -> Callable[[View], View]:
    """Authenticate a function view and name the one capability its page reads under.

    The page gate refuses a person who does not hold it before the view runs
    (``hq.platform.application.pages.page_capability``).
    """

    if not capability:
        raise ValueError("capability_required needs a capability name.")

    def decorate(view: View) -> View:
        guarded = login_required(view)
        guarded.__dict__["required_capability"] = capability
        return guarded

    return decorate


class CapabilityRequiredMixin(LoginRequiredMixin):
    """Authenticate a class-based view that names ``required_capability``, the one its page reads under.

    Left empty, the page gate refuses to serve the view rather than read it
    under READ.
    """

    required_capability = ""


__all__ = ["CapabilityRequiredMixin", "capability_required", "safe_next"]
