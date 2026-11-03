"""The identity provider's answer to a sign-in or a renewal (OIDC_CALLBACK_CLASS)."""

from typing import override

from django.contrib import auth
from mozilla_django_oidc.views import OIDCAuthenticationCallbackView

from hq.platform.application.request_identity import signed_in

from .oidc import SSO_FAILURE_SESSION_KEY


class HQCallbackView(OIDCAuthenticationCallbackView):
    """The provider's answer to a sign-in or renewal, ending the session it refuses.

    A renewal runs while the person is signed in, and the library leaves their
    session standing when HQ refuses its answer (a group removed, a token that
    does not verify), until it expires. Ended here, so a refusal takes effect
    at the renewal that met it.
    """

    @override
    def login_failure(self):
        # Only once the provider's answer was taken up, so a link to the callback signs nobody out.
        if "user" in self.__dict__ and signed_in(self.request) is not None:
            reason = self.request.session.get(SSO_FAILURE_SESSION_KEY)
            auth.logout(self.request)
            if reason:
                self.request.session[SSO_FAILURE_SESSION_KEY] = reason
        return super().login_failure()
