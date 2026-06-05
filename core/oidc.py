"""Pocket ID / OIDC authentication integration."""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied

from mozilla_django_oidc.auth import OIDCAuthenticationBackend


class HQOIDCAuthenticationBackend(OIDCAuthenticationBackend):
    """Map approved Pocket ID users onto Django users."""

    def verify_claims(self, claims):
        if not super().verify_claims(claims):
            return False

        email = claims.get("email", "").strip().lower()
        if not email:
            return False

        if claims.get("email_verified") is False:
            return False

        allowed_emails = settings.SEVERINO_OIDC_ALLOWED_EMAILS
        allowed_groups = settings.SEVERINO_OIDC_ALLOWED_GROUPS
        groups = set(claims.get("groups") or [])

        if allowed_emails or allowed_groups:
            return email in allowed_emails or bool(groups & allowed_groups)

        raise PermissionDenied(
            "SEVERINO_OIDC_ALLOWED_EMAILS or SEVERINO_OIDC_ALLOWED_GROUPS must be set."
        )

    def filter_users_by_claims(self, claims):
        email = claims.get("email", "").strip().lower()
        if not email:
            return self.UserModel.objects.none()

        users = self.UserModel.objects.filter(email__iexact=email)
        if users.exists():
            return users

        preferred_username = claims.get("preferred_username", "").strip()
        if preferred_username:
            return self.UserModel.objects.filter(username__iexact=preferred_username)

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
