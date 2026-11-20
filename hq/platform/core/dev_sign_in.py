"""Signing a browser in on a development server without typing a password (docs/Development.md).

Its token is signed under a salt of its own and names one account, and
redeeming it moves that account's last sign-in in one conditional update, so it
works once.
"""

from datetime import timedelta
from importlib import import_module

from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_not_required
from django.core import signing
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect
from django.utils import timezone
from django.views.decorators.http import require_safe

from .network import client_ip, parse_ip, socket_peer
from .write_bounds import Bound, writes

SALT = "hq.platform.core.dev_sign_in"
LIFETIME = timedelta(minutes=10)

SCRATCH_LINK = Bound(
    "scratch sign-in link",
    "one session and one sign-in row a link the scratch server printed, each link spent once",
)


def start_session(request: HttpRequest, user) -> None:
    """Sign ``user`` in on ``request``'s session, as a development server's own sign-in does."""

    login(request, user, backend=settings.AUTHENTICATION_BACKENDS[0])
    # With single sign-on on, an ID token with no expiry counts as expired
    # and the next page asks the provider again; this session has none.
    request.session["oidc_id_token_expiration"] = request.session.get_expiry_date().timestamp()


def new_session(user):
    """A saved session signed in as ``user``, for a client that sends its cookie."""

    request = HttpRequest()
    request.session = import_module(settings.SESSION_ENGINE).SessionStore()
    start_session(request, user)
    request.session.save()
    return request.session


def scratch_account():
    """The scratch server's throwaway account."""

    return get_user_model()._default_manager.get_by_natural_key(settings.SEVERINO_SCRATCH_ACCOUNT)


def mint(user) -> str:
    """A token that signs ``user`` in once, until ``LIFETIME`` has passed."""

    return signing.dumps([user.pk, _last(user.last_login)], salt=SALT, compress=False)


def redeem(token: str):
    """The scratch account, spent from ``token``, or None when the token signs nobody in."""

    try:
        pk, last = signing.loads(token, salt=SALT, max_age=LIFETIME)
    except signing.BadSignature, ValueError, TypeError:
        return None
    try:
        user = scratch_account()
    except get_user_model().DoesNotExist:
        return None
    if pk != user.pk or last != _last(user.last_login):
        return None
    # Spent here, against what the token names, so of two requests racing with
    # one token only the first signs in.
    spent = (
        get_user_model()
        ._default_manager.filter(pk=user.pk, last_login=user.last_login)
        .update(last_login=timezone.now())
    )
    if not spent:
        return None
    user.refresh_from_db(fields=["last_login"])
    return user


def _last(at) -> str:
    return at.isoformat() if at else ""


def _on_loopback(request: HttpRequest) -> bool:
    return all(
        (ip := parse_ip(address)) is not None and ip.is_loopback
        for address in (socket_peer(request), client_ip(request))
    )


@writes(SCRATCH_LINK)
@login_not_required
@require_safe
def scratch_sign_in(request: HttpRequest, token: str) -> HttpResponse:
    """Sign the scratch account in from the link the server printed, and land on the dashboard."""

    if not settings.SEVERINO_SCRATCH_ACCOUNT or not _on_loopback(request):
        raise Http404
    user = redeem(token)
    if user is None:
        return HttpResponseForbidden("This sign-in link is spent or expired. Restart the scratch server for a new one.")
    start_session(request, user)
    return redirect("dashboard")
