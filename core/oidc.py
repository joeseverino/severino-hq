"""Pocket ID / OIDC authentication integration."""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied

from mozilla_django_oidc.auth import OIDCAuthenticationBackend


class HQOIDCAuthenticationBackend(OIDCAuthenticationBackend):
    """Map approved Pocket ID users onto Django users."""

    def verify_claims(self, claims):
        print(f"DEBUG: OIDC Claims received: {claims}")
        if not super().verify_claims(claims):
            print("DEBUG: super().verify_claims(claims) failed")
            return False

        email = claims.get("email", "").strip().lower()
        if not email:
            print("DEBUG: No email in claims")
            return False

        # In homelab Pocket ID, email_verified might be False for passkey users.
        # We rely on the SSO provider's own authentication.
        if claims.get("email_verified") is False:
            print("DEBUG: email_verified is False (ignored for homelab)")

        allowed_emails = settings.SEVERINO_OIDC_ALLOWED_EMAILS
        allowed_groups = settings.SEVERINO_OIDC_ALLOWED_GROUPS
        groups = set(claims.get("groups") or [])
        print(f"DEBUG: Allowed emails: {allowed_emails}")
        print(f"DEBUG: Allowed groups: {allowed_groups}")
        print(f"DEBUG: User groups: {groups}")

        if allowed_emails or allowed_groups:
            result = email in allowed_emails or bool(groups & allowed_groups)
            print(f"DEBUG: verify_claims result: {result}")
            return result

        raise PermissionDenied(
            "SEVERINO_OIDC_ALLOWED_EMAILS or SEVERINO_OIDC_ALLOWED_GROUPS must be set."
        )

    def filter_users_by_claims(self, claims):
        email = claims.get("email", "").strip().lower()
        if not email:
            return self.UserModel.objects.none()

        users = self.UserModel.objects.filter(email__iexact=email)
        if users.exists():
            print(f"DEBUG: Found user by email: {users[0]}")
            return users

        preferred_username = claims.get("preferred_username", "").strip()
        if preferred_username:
            users = self.UserModel.objects.filter(username__iexact=preferred_username)
            if users.exists():
                print(f"DEBUG: Found user by username: {users[0]}")
                return users

        print("DEBUG: No matching user found")
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
