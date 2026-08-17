"""Authorization helpers for plugin-owned Django views."""

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ImproperlyConfigured, PermissionDenied

from application.security import AuthorizationError, web_principal


def _require(user, capability: str) -> None:
    try:
        web_principal(user).require(capability)
    except AuthorizationError as exc:
        raise PermissionDenied(str(exc)) from exc


def capability_required(capability: str):
    """Authenticate a function view and require one named capability."""

    if not capability:
        raise ValueError("capability_required needs a capability name.")

    def decorate(view):
        @login_required
        @wraps(view)
        def guarded(request, *args, **kwargs):
            _require(request.user, capability)
            return view(request, *args, **kwargs)

        return guarded

    return decorate


class CapabilityRequiredMixin(LoginRequiredMixin):
    """Authenticate a class-based view and require ``required_capability``."""

    required_capability = ""

    def get_required_capability(self) -> str:
        if not self.required_capability:
            raise ImproperlyConfigured(
                f"{type(self).__name__} must define required_capability."
            )
        return self.required_capability

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            _require(request.user, self.get_required_capability())
        return super().dispatch(request, *args, **kwargs)


__all__ = ["CapabilityRequiredMixin", "capability_required"]
