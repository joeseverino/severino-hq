"""Pocket ID / OIDC authentication integration."""

import logging
from typing import Any, override
lazy from urllib.parse import urlsplit

import requests
from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, get_user_model
from django.core.exceptions import PermissionDenied, SuspiciousOperation
from mozilla_django_oidc.auth import OIDCAuthenticationBackend
from mozilla_django_oidc.middleware import SessionRefresh
lazy from django.utils import timezone
lazy from django.utils.http import url_has_allowed_host_and_scheme

lazy from hq.platform.application.avatars import SESSION_KEY, remember_avatar
lazy from hq.platform.application.linked_accounts import SIGN_IN, bind_sign_in, record_claimed_accounts, sign_in_subject
lazy from hq.platform.core.audit import record_event
lazy from hq.platform.core.models import AuditLog
lazy from hq.platform.core.outbound import allowed

logger = logging.getLogger("severino.auth")


TAILSCALE_PRINCIPAL_CLAIM = "tailscale_principal"
TAILSCALE_PRINCIPAL_SESSION_KEY = "oidc_tailscale_principal"
# Why the last sign-in did not finish, for the page it lands on.
SSO_FAILURE_SESSION_KEY = "oidc_failure"
# When this session's person signed in. A session is renewed with the
# provider every few minutes and each renewal is a login to Django, so the
# account's last login says only that the session is alive.
SIGNED_IN_SESSION_KEY = "signed_in_at"


def sso_only() -> bool:
    """Whether no password backend is installed."""

    backends = tuple(settings.AUTHENTICATION_BACKENDS)
    return bool(backends) and not any(name.endswith("ModelBackend") for name in backends)


def _tailscale_principal(payload) -> str:
    value = payload.get(TAILSCALE_PRINCIPAL_CLAIM)
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value if value and len(value) <= 254 else ""


class HQOIDCAuthenticationBackend(OIDCAuthenticationBackend):
    """Map approved Pocket ID users onto Django users."""

    # The claims get_userinfo read in this sign-in.
    _userinfo: dict[str, Any] | None = None

    @override
    def authenticate(self, request, **kwargs):
        """A provider that refuses the exchange is a failed sign-in, not a crash.

        The library raises out of ``authenticate()`` when the token endpoint
        answers an error, which Django turns into a 500. And the failure URL
        is the login page, which with single sign-on only goes straight back to
        the provider, so it has to be told why it stopped (``sso_failed``).
        """

        try:
            with allowed("oidc"):
                return super().authenticate(request, **kwargs)
        except requests.RequestException as exc:
            status = exc.response.status_code if exc.response is not None else None
            reason = (
                "Pocket ID refused HQ's client credentials: the client secret HQ "
                "holds is not the one Pocket ID issued for this client."
                if status in (400, 401)
                else "HQ could not reach Pocket ID to finish signing in."
            )
            logger.warning(reason, extra={"event": "auth.sso.exchange_failed", "status": status})

            record_event(action=AuditLog.Action.DENIED, type_label="Sign-in", message=reason)
            # The library raises only once it has a request to read the code from.
            request.session[SSO_FAILURE_SESSION_KEY] = reason
            return None

    @override
    def verify_token(self, token, **kwargs):
        """Check who the token was minted for, which the library does not.

        `mozilla_django_oidc` decodes with `verify_aud: False` and passes no
        issuer, so every RS256 token signed by a key in the provider's JWKS
        verifies here, including one minted for a different client. HQ's own
        machine API already pins both; this is the browser path meeting it.
        """

        payload = super().verify_token(token, **kwargs)
        audience = payload.get("aud")
        allowed = {audience} if isinstance(audience, str) else set(audience or ())
        client_id = settings.OIDC_RP_CLIENT_ID
        if client_id not in allowed:
            raise SuspiciousOperation("The ID token was issued for another client.")
        if len(allowed) > 1 and payload.get("azp") != client_id:
            raise SuspiciousOperation("The ID token was authorized for another party.")
        issuer = settings.OIDC_ISSUER
        if not issuer or payload.get("iss") != issuer:
            raise SuspiciousOperation("The ID token came from another issuer.")
        return payload

    @override
    def get_userinfo(self, access_token, id_token, payload):
        """Kept for the rest of this sign-in: the picture claim is read from it."""

        self._userinfo = super().get_userinfo(access_token, id_token, payload)
        return self._userinfo

    @override
    def get_or_create_user(self, access_token, id_token, payload):
        user = super().get_or_create_user(access_token, id_token, payload)
        if user is None:
            return None
        self._remember_picture(user, access_token)

        bind_sign_in(user, self._subject_key(payload))
        record_claimed_accounts(user, payload)
        # authenticate() holds the callback's request, whose session the library reads too.
        session = self.request.session
        session.setdefault(SIGNED_IN_SESSION_KEY, timezone.now().isoformat())
        principal = _tailscale_principal(payload)
        if principal:
            session[TAILSCALE_PRINCIPAL_SESSION_KEY] = principal
        else:
            session.pop(TAILSCALE_PRINCIPAL_SESSION_KEY, None)
        return user

    def _remember_picture(self, user, access_token) -> None:
        """Keep the person's picture, and tell the session which one it is.

        Never a reason for a sign-in to fail: whatever goes wrong here, the
        person is still who the provider said, without a picture.
        """

        try:
            claims = self._userinfo or {}
            digest = remember_avatar(
                user,
                str(claims.get("picture") or "").strip(),
                issuer=settings.OIDC_ISSUER,
                access_token=access_token or "",
            )
        except Exception:  # noqa: BLE001 - a picture must not stop a sign-in
            logger.warning("The picture could not be kept.", extra={"event": "auth.avatar.failed"})
            return
        if digest:
            self.request.session[SESSION_KEY] = digest
        else:
            self.request.session.pop(SESSION_KEY, None)

    @override
    def verify_claims(self, claims):
        preferred_username = claims.get("preferred_username", "").strip()
        email = claims.get("email", "").strip().lower()
        if not preferred_username and not email:
            return False
        if not self._subject_key(claims):
            return False

        allowed_emails = settings.SEVERINO_OIDC_ALLOWED_EMAILS
        allowed_groups = settings.SEVERINO_OIDC_ALLOWED_GROUPS
        groups = set(claims.get("groups") or [])

        if allowed_emails or allowed_groups:
            # An unverified address is a claim the person made about
            # themselves, not one the provider stands behind.
            verified_email = email if claims.get("email_verified") is True else ""
            return bool(groups & allowed_groups) or (bool(verified_email) and verified_email in allowed_emails)

        raise PermissionDenied("Nobody can sign in until HQ's settings name the people or groups allowed in.")

    @staticmethod
    def _subject_key(claims) -> str:
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            return ""
        return sign_in_subject(settings.OIDC_ISSUER, subject.strip())

    @override
    def filter_users_by_claims(self, claims):
        """The user this subject signed in as before; else one not yet bound.

        A name or an address is something a person can change, so it only
        introduces a subject to an account nobody has signed in to by subject
        yet, and an address only when the provider verified it. After that the
        subject alone decides, so renaming someone does not hand them another
        person's account.
        """

        key = self._subject_key(claims)
        if not key:
            return self.UserModel.objects.none()
        bound = self.UserModel.objects.filter(linked_accounts__provider=SIGN_IN, linked_accounts__login=key)
        if bound.exists():
            return bound
        unbound = self.UserModel.objects.exclude(linked_accounts__provider=SIGN_IN)

        preferred_username = claims.get("preferred_username", "").strip()
        if preferred_username:
            users = unbound.filter(username__iexact=preferred_username)
            if users.exists():
                return users

        email = claims.get("email", "").strip().lower()
        if email and claims.get("email_verified") is True:
            users = unbound.filter(email__iexact=email)
            if users.exists():
                return users

        return self.UserModel.objects.none()

    @override
    def create_user(self, claims):
        email = claims.get("email", "").strip().lower()
        username = claims.get("preferred_username", "").strip() or email.split("@", 1)[0] or claims.get("sub", "")
        username = self._unique_username(username)

        user = self.UserModel.objects.create_user(
            username=username,
            email=email,
            first_name=claims.get("given_name", "")[:150],
            last_name=claims.get("family_name", "")[:150],
        )
        user.set_unusable_password()
        user.save(update_fields=["password"])
        return user

    @override
    def update_user(self, user, claims):
        changed = []
        mappings = {
            "email": claims.get("email", "").strip().lower(),
            "first_name": claims.get("given_name", "")[:150],
            "last_name": claims.get("family_name", "")[:150],
        }
        for field, value in mappings.items():
            if value and getattr(user, field) != value:
                setattr(user, field, value)
                changed.append(field)
        if changed:
            user.save(update_fields=changed)
        return user

    def _unique_username(self, base):
        base = (base or "oidc-user")[:140]
        User = get_user_model()
        candidate = base
        suffix = 2
        while User.objects.filter(username=candidate).exists():
            candidate = f"{base[:140]}-{suffix}"
            suffix += 1
        return candidate


class HQSessionRefresh(SessionRefresh):
    """Renews an aging session, and returns the operator to the page they were on.

    The provider sends a renewed session back to the address of the request
    that asked for renewal. For a background request (a poll, a deferred panel)
    that address is a JSON endpoint or a probe, so the operator would land on
    it. A background request names the page it serves in its Referer; renewal
    returns there, or to the dashboard when there is no same-site page.
    """

    # Probes answer without a session, so renewing one through them means nothing.
    PROBES = ("health_live", "health_ready")

    def __init__(self, get_response):
        super().__init__(get_response)
        self.OIDC_EXEMPT_URLS = [*self.OIDC_EXEMPT_URLS, *self.PROBES]

    @override
    def process_request(self, request):
        # A session signed in through a backend this release does not have is
        # signed out: Django already reads it so. It is ended here, before the
        # refresh tries to import a path that was renamed or removed.
        backend = request.session.get(BACKEND_SESSION_KEY)
        if backend and backend not in settings.AUTHENTICATION_BACKENDS:
            request.session.flush()
        response = super().process_request(request)
        if response is not None and request.headers.get("x-requested-with") == "XMLHttpRequest":
            request.session["oidc_login_next"] = _page_behind(request)
        return response


def _page_behind(request) -> str:
    referer = request.headers.get("referer", "")
    if url_has_allowed_host_and_scheme(referer, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        parts = urlsplit(referer)
        return parts.path + (f"?{parts.query}" if parts.query else "")
    return "/"
