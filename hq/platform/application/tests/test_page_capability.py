"""A page opens only for a person whose role holds the capability it reads under."""

from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponse
from django.test import RequestFactory, TestCase
from django.views.generic import View

from hq.platform.application import roles
from hq.platform.core.middleware import LoginRequiredMiddleware
from hq.platform.core.testing import person
from hq_sdk.web import CapabilityRequiredMixin

# Written into the log by the test, so a refused page that read the log would show it.
SECRET = "example-only-the-audit-log-holds-this"


def _person(username: str, role: str):
    user = person(username=username)
    if role:
        roles.assign(user, role)
    return user


class _Unnamed(CapabilityRequiredMixin, View):
    def get(self, _request):
        return HttpResponse(SECRET)


def _gate(user, view):
    """What the page gate does with ``user`` at ``view``: None to let it run, or its refusal raised."""

    request = RequestFactory().get("/example/")
    request.user = user
    return LoginRequiredMiddleware(lambda _request: HttpResponse()).process_view(request, view, (), {})


class ExtensionViewTests(TestCase):
    """An extension's view, by the mixin or the decorator ``hq_sdk.web`` gives it, is refused by the one page gate."""

    def test_a_mixin_view_that_names_no_capability_is_never_served(self):
        with self.assertRaises(ImproperlyConfigured):
            _gate(_person("operator", roles.Role.OPERATOR), _Unnamed.as_view())
