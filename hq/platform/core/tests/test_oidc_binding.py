"""A sign-in reaches an account HQ already holds only through an address the provider verified."""

from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from hq.platform.application.linked_accounts import SIGN_IN, sign_in_subject
from hq.platform.core.oidc import HQOIDCAuthenticationBackend

User = get_user_model()
ISSUER = "https://id.example.test"


def _signed_in(claims):
    """The user the backend signs ``claims`` in as, with the provider's userinfo answering ``claims``."""

    backend = HQOIDCAuthenticationBackend()
    backend.request = type("Request", (), {"session": {}})()
    with patch.object(HQOIDCAuthenticationBackend, "get_userinfo", return_value=claims):
        return backend.get_or_create_user("access-token", "id-token", claims)


@override_settings(
    OIDC_ISSUER=ISSUER, SEVERINO_OIDC_ALLOWED_EMAILS={}, SEVERINO_OIDC_ROLE_GROUPS={"admins": "operator"}
)
class FirstBindTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username="joe", email="joe@example.com")

    def test_a_matching_username_takes_no_account(self):
        user = _signed_in({"sub": "subject-1", "preferred_username": "joe", "groups": ["admins"]})

        self.assertNotEqual(user, self.admin)
        self.assertFalse(self.admin.linked_accounts.filter(provider=SIGN_IN).exists())

    def test_an_unverified_address_takes_no_account(self):
        for verified in (None, False, "true"):
            claims = {"sub": f"subject-{verified}", "preferred_username": "joe", "email": "JOE@example.com"}
            if verified is not None:
                claims["email_verified"] = verified
            with self.subTest(email_verified=verified):
                user = _signed_in({**claims, "groups": ["admins"]})

                self.assertNotEqual(user, self.admin)
                self.assertEqual(user.email, "")
        self.assertFalse(self.admin.linked_accounts.filter(provider=SIGN_IN).exists())

    def test_a_verified_address_binds_the_local_admin_on_a_groups_only_deployment(self):
        claims = {"sub": "subject-1", "email": "Joe@Example.com", "email_verified": True, "groups": ["admins"]}

        user = _signed_in(claims)

        self.assertEqual(user, self.admin)
        self.assertEqual(self.admin.linked_accounts.get(provider=SIGN_IN).login, sign_in_subject(ISSUER, "subject-1"))

    def test_the_address_is_asked_for_whatever_the_allowlist_holds(self):
        self.assertIn("email", settings.OIDC_RP_SCOPES.split())

    @override_settings(OIDC_CREATE_USER=False)
    def test_with_no_new_accounts_an_unverified_address_is_refused(self):
        user = _signed_in({"sub": "subject-1", "email": "joe@example.com", "groups": ["admins"]})

        self.assertIsNone(user)


@override_settings(
    OIDC_ISSUER=ISSUER, SEVERINO_OIDC_ALLOWED_EMAILS={}, SEVERINO_OIDC_ROLE_GROUPS={"admins": "operator"}
)
class AddressCopyTests(TestCase):
    def test_a_new_account_keeps_only_a_verified_address(self):
        person = {"preferred_username": "sam", "email": "sam@example.com", "groups": ["admins"]}

        verified = _signed_in({**person, "sub": "subject-1", "email_verified": True})
        unverified = _signed_in({**person, "sub": "subject-2"})

        self.assertEqual(verified.email, "sam@example.com")
        self.assertEqual(unverified.email, "")

    def test_a_changed_address_is_copied_only_when_verified(self):
        claims = {"sub": "subject-1", "email": "sam@example.com", "email_verified": True, "groups": ["admins"]}
        user = _signed_in(claims)

        _signed_in({**claims, "email": "taken@example.com", "email_verified": False})
        user.refresh_from_db()
        self.assertEqual(user.email, "sam@example.com")

        _signed_in({**claims, "email": "new@example.com"})
        user.refresh_from_db()
        self.assertEqual(user.email, "new@example.com")


@override_settings(
    OIDC_ISSUER=ISSUER, SEVERINO_OIDC_ALLOWED_EMAILS={}, SEVERINO_OIDC_ROLE_GROUPS={"admins": "operator"}
)
class OneAccountOnePersonTests(TestCase):
    def test_an_account_another_person_claims_is_not_recorded_and_the_sign_in_goes_on(self):
        claims = {"preferred_username": "sam", "groups": ["admins"], "github": "example-user"}
        first = _signed_in({**claims, "sub": "subject-1"})

        with self.assertLogs("severino.auth", "WARNING"):
            second = _signed_in({**claims, "sub": "subject-2", "preferred_username": "kim"})

        self.assertNotEqual(second, first)
        self.assertEqual(first.linked_accounts.get(provider="github").login, "example-user")
        self.assertFalse(second.linked_accounts.filter(provider="github").exists())
        self.assertTrue(second.linked_accounts.filter(provider=SIGN_IN).exists())
