"""Pocket ID / OIDC authentication integration."""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, SuspiciousOperation

import logging

import requests
from mozilla_django_oidc.auth import OIDCAuthenticationBackend
from mozilla_django_oidc.middleware import SessionRefresh

logger = logging.getLogger("severino.auth")


TAILSCALE_PRINCIPAL_CLAIM = "tailscale_principal"
TAILSCALE_PRINCIPAL_SESSION_KEY = "oidc_tailscale_principal"
# Why the last sign-in did not finish, for the page it lands on.
SSO_FAILURE_SESSION_KEY = "oidc_failure"


def _tailscale_principal(payload) -> str:
    value = payload.get(TAILSCALE_PRINCIPAL_CLAIM)
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value if value and len(value) <= 254 else ""


class HQOIDCAuthenticationBackend(OIDCAuthenticationBackend):
    """Map approved Pocket ID users onto Django users."""

    def authenticate(self, request, **kwargs):
        """A provider that refuses the exchange is a failed sign-in, not a crash.

        The library raises out of ``authenticate()`` when the token endpoint
        answers an error, which Django turns into a 500. And the failure URL
        is the login page, which with single sign-on only goes straight back to
        the provider, so it has to be told why it stopped (``sso_failed``).
        """

        try:
            return super().authenticate(request, **kwargs)
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            reason = (
                "Pocket ID refused HQ's client credentials: the client secret HQ "
                "holds is not the one Pocket ID issued for this client."
                if status in (400, 401)
                else "HQ could not reach Pocket ID to finish signing in."
            )
            logger.warning(reason, extra={"event": "auth.sso.exchange_failed", "status": status})
            from core.audit import record_event
            from core.models import AuditLog

            record_event(action=AuditLog.Action.DENIED, type_label="Sign-in", message=reason)
            session = getattr(request, "session", None)
            if session is not None:
                session[SSO_FAILURE_SESSION_KEY] = reason
            return None

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
        issuer = getattr(settings, "OIDC_ISSUER", "")
        if not issuer or payload.get("iss") != issuer:
            raise SuspiciousOperation("The ID token came from another issuer.")
        return payload

    def get_or_create_user(self, access_token, id_token, payload):
        user = super().get_or_create_user(access_token, id_token, payload)
        if user is None:
            return None
        from application.linked_accounts import bind_sign_in, record_claimed_accounts

        bind_sign_in(user, self._subject_key(payload))
        record_claimed_accounts(user, payload)
        session = getattr(getattr(self, "request", None), "session", None)
        if session is not None:
            principal = _tailscale_principal(payload)
            if principal:
                session[TAILSCALE_PRINCIPAL_SESSION_KEY] = principal
            else:
                session.pop(TAILSCALE_PRINCIPAL_SESSION_KEY, None)
        return user

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
            return bool(groups & allowed_groups) or (
                bool(verified_email) and verified_email in allowed_emails
            )

        raise PermissionDenied(
            "SEVERINO_OIDC_ALLOWED_EMAILS or SEVERINO_OIDC_ALLOWED_GROUPS must be set."
        )

    @staticmethod
    def _subject_key(claims) -> str:
        from application.linked_accounts import sign_in_subject

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            return ""
        return sign_in_subject(getattr(settings, "OIDC_ISSUER", ""), subject.strip())

    def filter_users_by_claims(self, claims):
        """The user this subject signed in as before; else one not yet bound.

        A name or an address is something a person can change, so it only
        introduces a subject to an account nobody has signed in to by subject
        yet, and an address only when the provider verified it. After that the
        subject alone decides, so renaming someone does not hand them another
        person's account.
        """

        from application.linked_accounts import SIGN_IN

        key = self._subject_key(claims)
        if not key:
            return self.UserModel.objects.none()
        bound = self.UserModel.objects.filter(
            linked_accounts__provider=SIGN_IN, linked_accounts__login=key
        )
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

    def create_user(self, claims):
        email = claims.get("email", "").strip().lower()
        username = (
            claims.get("preferred_username", "").strip()
            or email.split("@", 1)[0]
            or claims.get("sub", "")
        )
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

    def process_request(self, request):
        response = super().process_request(request)
        if response is not None and request.headers.get("x-requested-with") == "XMLHttpRequest":
            request.session["oidc_login_next"] = _page_behind(request)
        return response


def _page_behind(request) -> str:
    from urllib.parse import urlsplit

    from django.utils.http import url_has_allowed_host_and_scheme

    referer = request.headers.get("referer", "")
    if url_has_allowed_host_and_scheme(
        referer, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        parts = urlsplit(referer)
        return parts.path + (f"?{parts.query}" if parts.query else "")
    return "/"
