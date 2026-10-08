"""Authorization and navigation helpers for plugin-owned Django views."""

from collections.abc import Callable
from functools import wraps
from typing import TYPE_CHECKING, Any, Concatenate, override

from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.http import HttpRequest, HttpResponseBase

from hq.platform.application.security import AuthorizationError, safe_next, web_principal

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser
    from django.contrib.auth.models import AnonymousUser

View = Callable[Concatenate[HttpRequest, ...], HttpResponseBase]


def _require(user: AbstractBaseUser | AnonymousUser, capability: str) -> None:
    try:
        web_principal(user).require(capability)
    except AuthorizationError as exc:
        raise PermissionDenied(str(exc)) from exc


def capability_required(capability: str) -> Callable[[View], View]:
    """Authenticate a function view and require one named capability."""

    if not capability:
        raise ValueError("capability_required needs a capability name.")

    def decorate(view: View) -> View:
        @login_required
        @wraps(view)
        def guarded(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
            _require(request.user, capability)
            return view(request, *args, **kwargs)

        return guarded

    return decorate


class CapabilityRequiredMixin(LoginRequiredMixin):
    """Authenticate a class-based view and require ``required_capability``."""

    required_capability = ""

    def get_required_capability(self) -> str:
        if not self.required_capability:
            raise ImproperlyConfigured(f"{type(self).__name__} must define required_capability.")
        return self.required_capability

    @override
    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if request.user.is_authenticated:
            _require(request.user, self.get_required_capability())
        return super().dispatch(request, *args, **kwargs)


__all__ = ["CapabilityRequiredMixin", "capability_required", "safe_next"]
