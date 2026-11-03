"""The identity provider's back-channel logout: it signs a person out of HQ the moment it removes them.

Pocket ID posts a logout token (OpenID Connect Back-Channel Logout 1.0) when a
person is signed out there or loses the group HQ's client admits. It names
the person by `sub` alone, with no session id, so every HQ session of theirs
ends. It retries only a 408, a 429 or a 5xx, so a token HQ refuses is a 400.
"""

import logging

from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseServerError
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from hq.platform.application import tokens
from hq.platform.application.capabilities import run_capability
from hq.platform.application.security import internal_principal
from hq.platform.application.sessions import SIGN_OUT

from .write_bounds import NOTHING, writes

logger = logging.getLogger("severino.auth")

LOGOUT_TOKEN_TYPES = frozenset({"logout+jwt"})
LOGOUT_EVENT = "http://schemas.openid.net/event/backchannel-logout"


# Exempt from CSRF and a session: the provider's server posts it, and the signed token is its credential,
# so a caller without one is refused before anything is written.
@writes(NOTHING)
@csrf_exempt
@login_not_required
@require_POST
def backchannel_logout(request):
    token = request.POST.get("logout_token", "")
    try:
        claims = tokens.decode(
            token,
            tokens.signing_key(token),
            audience=settings.OIDC_RP_CLIENT_ID,
            types=LOGOUT_TOKEN_TYPES,
        )
    except tokens.TokenError:
        return HttpResponseBadRequest("The logout token was not accepted.")
    subject = claims.get("sub")
    events = claims.get("events")
    # What a logout token holds that decode does not ask of every token
    # (section 2.6): its event, no nonce, so an ID token is never one, and a subject.
    if not isinstance(events, dict) or LOGOUT_EVENT not in events or "nonce" in claims:
        logger.warning("Rejected a logout token that is not one.", extra={"event": "auth.logout.rejected"})
        return HttpResponseBadRequest("The logout token was not accepted.")
    if not isinstance(subject, str) or not subject.strip():
        logger.warning("Rejected a logout token naming nobody.", extra={"event": "auth.logout.rejected"})
        return HttpResponseBadRequest("The logout token was not accepted.")
    jti = claims.get("jti")
    # Required by section 2.4, and the retry key that makes a redelivered token act once.
    if not isinstance(jti, str) or not jti:
        logger.warning("Rejected a logout token with no jti.", extra={"event": "auth.logout.rejected"})
        return HttpResponseBadRequest("The logout token was not accepted.")
    ran = run_capability(
        SIGN_OUT,
        {"subject": subject.strip()},
        principal=internal_principal("oidc-backchannel-logout"),
        idempotency_key=jti,
    )
    if ran.answer.get("ok"):
        return HttpResponse(status=200)
    code = (ran.answer.get("error") or {}).get("code")
    logger.warning("A logout token was not acted on: %s.", code, extra={"event": "auth.logout.rejected"})
    # A jti that is no retry key, or one already spent on another subject, is the
    # token's fault; anything else is HQ's, which the provider retries.
    if code in ("invalid_idempotency_key", "idempotency_conflict"):
        return HttpResponseBadRequest("The logout token was not accepted.")
    return HttpResponseServerError("The logout could not be completed.")
