"""Pocket ID / OIDC authentication integration."""

import logging
from typing import Any, override
lazy from urllib.parse import urlsplit

import requests
from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, get_user_model
from django.core.exceptions import PermissionDenied
from mozilla_django_oidc.auth import OIDCAuthenticationBackend
from mozilla_django_oidc.middleware import SessionRefresh
lazy from django.utils import timezone
lazy from django.utils.http import url_has_allowed_host_and_scheme

lazy from hq.platform.application import roles, tokens
lazy from hq.platform.application.avatars import SESSION_KEY, remember_avatar
lazy from hq.platform.application.linked_accounts import SIGN_IN, bind_sign_in, record_claimed_accounts, sign_in_subject
lazy from hq.platform.core.audit import record_event
lazy from hq.platform.core.models import AuditLog
lazy from hq.platform.core.outbound import allowed

from .write_bounds import Bound

logger = logging.getLogger("severino.auth")


TAILSCALE_PRINCIPAL_CLAIM = "tailscale_principal"
TAILSCALE_PRINCIPAL_SESSION_KEY = "oidc_tailscale_principal"
# Why the last sign-in did not finish, for the page it lands on.
SSO_FAILURE_SESSION_KEY = "oidc_failure"
# When this session's person signed in. A session is renewed with the
# provider every few minutes and each renewal is a login to Django, so the
# account's last login says only that the session is alive.
SIGNED_IN_SESSION_KEY = "signed_in_at"

# A stranger who starts a handshake, or answers one HQ did not start, gets a
# session row; a refused sign-in's audit row needs the provider's own answer.
HANDSHAKE = Bound(
    "sign-in handshake",
    "one session row a handshake, deleted each day once it has expired",
)


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


def _verified_email(claims) -> str:
    """The address the provider stands behind, or "": an unverified one is the person's own say-so."""

    email = claims.get("email")
    if not isinstance(email, str) or claims.get("email_verified") is not True:
        return ""
    return email.strip().lower()


def _refuse(request, reason: str) -> None:
    """Audit a refused sign-in and keep its reason for the page it lands on.

    Every refusal comes through here, since the login page reads a sign-in
    with no reason as one the person cancelled.
    """

    record_event(action=AuditLog.Action.DENIED, type_label="Sign-in", message=reason)
    request.session[SSO_FAILURE_SESSION_KEY] = reason


class HQOIDCAuthenticationBackend(OIDCAuthenticationBackend):
    """Map approved Pocket ID users onto Django users."""

    # The claims get_userinfo read in this sign-in.
    _userinfo: dict[str, Any] | None = None
    # The role verify_claims resolved for this sign-in, which get_or_create_user writes.
    _role = ""

    @override
    def authenticate(self, request, **kwargs):
        """A provider that refuses the exchange, or a token HQ refuses, is a failed sign-in, not a crash.

        The library raises out of ``authenticate()`` when the token endpoint
        answers an error, which Django turns into a 500. And the failure URL
        is the login page, which with single sign-on only goes straight back to
        the provider, so it has to be told why it stopped (``sso_failed``).
        The library raises only once it has a request to read the code from.
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
        except tokens.TokenError:
            reason = "HQ could not verify the sign-in Pocket ID sent. Sign in again."
        _refuse(request, reason)
        return None

    @override
    def retrieve_matching_jwk(self, token):
        """The key the machine API holds, so a sign-in never fetches the key set.

        A key not held is a refused sign-in, and a read of the key set asked
        for, so the person's next try finds it.
        """

        return tokens.signing_key(token)

    @override
    def get_payload_data(self, token, key):
        """The claims of an ID token for this client, checked as every token HQ is handed.

        The library decodes with `verify_aud: False` and no issuer; this is
        the machine API's one check, with HQ's client as the audience. OpenID
        Connect adds `azp` to its ID token validation, which PyJWT does not read.
        """

        client_id = settings.OIDC_RP_CLIENT_ID
        payload = tokens.decode(token, key, audience=client_id, types=tokens.ID_TOKEN_TYPES)
        audience = payload["aud"]
        if not isinstance(audience, str) and len(audience) > 1 and payload.get("azp") != client_id:
            logger.warning("Rejected an ID token authorized for another party.", extra={"event": "api.token.rejected"})
            raise tokens.TokenError("The ID token was authorized for another party.")
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
        if self._role:
            roles.assign(user, self._role)
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
        if (not preferred_username and not email) or not self._subject_key(claims):
            _refuse(self.request, "Pocket ID did not say who this account is, so HQ cannot sign it in.")
            return False

        if not settings.SEVERINO_OIDC_ROLE_GROUPS and not settings.SEVERINO_OIDC_ALLOWED_EMAILS:
            reason = "Nobody can sign in until HQ's settings name the people or groups allowed in."
            _refuse(self.request, reason)
            raise PermissionDenied(reason)
        self._role = resolved_role(claims)
        if not self._role:
            _refuse(
                self.request, "Your account is in no group HQ admits. An operator can add you to one, then sign in."
            )
        return bool(self._role)

    @override
    def get_user(self, user_id):
        """The session's person, with their role, so a request reads it without a query of its own."""

        # get, as the library's own does: first() would order a lookup by key.
        try:
            return self.UserModel.objects.select_related("person_role").get(pk=user_id)
        except self.UserModel.DoesNotExist:
            return None

    @staticmethod
    def _subject_key(claims) -> str:
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            return ""
        return sign_in_subject(settings.OIDC_ISSUER, subject.strip())

    @override
    def filter_users_by_claims(self, claims):
        """The user this subject signed in as before; else one not yet bound, by verified address.

        A name is something a person can change and an unverified address is
        one they typed, so neither finds an account. An address the provider
        verified introduces a subject to an account nobody has signed in to by
        subject yet; after that the subject alone decides, so renaming someone
        does not hand them another person's account.
        """

        key = self._subject_key(claims)
        if not key:
            return self.UserModel.objects.none()
        bound = self.UserModel.objects.filter(linked_accounts__provider=SIGN_IN, linked_accounts__login=key)
        if bound.exists():
            return bound
        email = _verified_email(claims)
        if not email:
            return self.UserModel.objects.none()
        return self.UserModel.objects.exclude(linked_accounts__provider=SIGN_IN).filter(email__iexact=email)

    @override
    def create_user(self, claims):
        email = _verified_email(claims)
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
            "email": _verified_email(claims),
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


def resolved_role(claims) -> str:
    """The highest role the person's groups and verified address are given, or "" for none.

    A group HQ's settings do not name, and a `groups` claim that is missing or
    not a list, give nothing.
    """

    groups = claims.get("groups")
    named = settings.SEVERINO_OIDC_ROLE_GROUPS
    held = (
        [named[group] for group in groups if isinstance(group, str) and group in named]
        if isinstance(groups, list)
        else []
    )
    email = _verified_email(claims)
    if email in settings.SEVERINO_OIDC_ALLOWED_EMAILS:
        held.append(settings.SEVERINO_OIDC_ALLOWED_EMAILS[email])
    return roles.highest(held)


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
