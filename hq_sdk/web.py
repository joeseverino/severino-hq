"""For an extension's Django views: name the capability a person must hold to open one."""

from collections.abc import Callable
from typing import Concatenate

from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponseBase

View = Callable[Concatenate[HttpRequest, ...], HttpResponseBase]


def capability_required(capability: str) -> Callable[[View], View]:
    """Authenticate a function view and name the capability its page reads under, beside READ.

    The page gate refuses a person who does not hold both before the view runs
    (``hq.platform.application.pages.page_capabilities``).
    """

    if not capability:
        raise ValueError("capability_required needs a capability name.")

    def decorate(view: View) -> View:
        guarded = login_required(view)
        guarded.__dict__["required_capability"] = capability
        return guarded

    return decorate


class CapabilityRequiredMixin(LoginRequiredMixin):
    """Authenticate a class-based view that names ``required_capability``, which its page reads under beside READ.

    Left empty, the page gate refuses to serve the view rather than read it
    under READ alone.
    """

    required_capability = ""


__all__ = ["CapabilityRequiredMixin", "capability_required"]
