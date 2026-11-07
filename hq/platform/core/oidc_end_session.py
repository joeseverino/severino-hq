"""Where HQ's Sign out sends a person signed in through the identity provider: its end-session endpoint.

Ending HQ's session alone leaves Pocket ID's standing, and the next visit signs
straight back in. ``OIDC_OP_LOGOUT_URL_METHOD`` names this function, which the
library's ``OIDCLogoutView`` calls before it ends HQ's session (OpenID Connect
RP-Initiated Logout 1.0).
"""

from urllib.parse import urlencode

from django.conf import settings
from django.http import HttpRequest
from django.shortcuts import resolve_url
from mozilla_django_oidc.utils import absolutify

ID_TOKEN_SESSION_KEY = "oidc_id_token"  # noqa: S105 - a session key's name, no credential


def end_session_url(request: HttpRequest) -> str:
    """The provider's end-session address for this session, or HQ's own landing when it holds no ID token.

    A password or CLI session never had one, so it is signed out of HQ alone.
    """

    landing = resolve_url(settings.LOGOUT_REDIRECT_URL)
    id_token = request.session.get(ID_TOKEN_SESSION_KEY)
    if not id_token:
        return landing
    query = urlencode(
        {
            "id_token_hint": id_token,
            "client_id": settings.OIDC_RP_CLIENT_ID,
            # Pocket ID sends the person back here only when it is one of the
            # client's registered Logout Callback URLs (docs/Security.md).
            "post_logout_redirect_uri": absolutify(request, landing),
        }
    )
    return f"{settings.OIDC_OP_LOGOUT_ENDPOINT}?{query}"
