"""Pocket ID / OIDC authentication integration."""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, SuspiciousOperation

from mozilla_django_oidc.auth import OIDCAuthenticationBackend


TAILSCALE_PRINCIPAL_CLAIM = "tailscale_principal"
TAILSCALE_PRINCIPAL_SESSION_KEY = "oidc_tailscale_principal"


def _tailscale_principal(payload) -> str:
    value = payload.get(TAILSCALE_PRINCIPAL_CLAIM)
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value if value and len(value) <= 254 else ""


class HQOIDCAuthenticationBackend(OIDCAuthenticationBackend):
    """Map approved Pocket ID users onto Django users."""

    def verify_token(self, token, **kwargs):
        """Check who the token was minted for, which the library does not.

        `mozilla_django_oidc` decodes with `verify_aud: False` and passes no
        issuer, so every RS256 token signed by a key in the provider's JWKS
        verifies here -- including one minted for a different client. HQ's own
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
        if issuer and payload.get("iss") != issuer:
            raise SuspiciousOperation("The ID token came from another issuer.")
        return payload

    def get_or_create_user(self, access_token, id_token, payload):
        user = super().get_or_create_user(access_token, id_token, payload)
        if user is None:
            return None
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

    def filter_users_by_claims(self, claims):
        preferred_username = claims.get("preferred_username", "").strip()
        if preferred_username:
            users = self.UserModel.objects.filter(username__iexact=preferred_username)
            if users.exists():
                return users

        email = claims.get("email", "").strip().lower()
        if email:
            users = self.UserModel.objects.filter(email__iexact=email)
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
