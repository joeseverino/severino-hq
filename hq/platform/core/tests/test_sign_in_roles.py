"""A person's role comes from their identity provider's groups, resolved at each sign-in and renewal."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import SuspiciousOperation
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from hq.platform.api.tests.tokens import ISSUER, _serving
from hq.platform.application import roles
from hq.platform.application.linked_accounts import bind_sign_in, sign_in_subject
from hq.platform.application.security import Capability, web_principal
from hq.platform.core.audit import audited_labels
from hq.platform.core.models import AuditLog, PersonRole
from hq.platform.core.oidc import SSO_FAILURE_SESSION_KEY, HQOIDCAuthenticationBackend
from hq.platform.core.test_runner import no_default_role
from hq.platform.core.tests.test_id_token import CLIENT, NONCE, _id_token

User = get_user_model()
BACKEND = "hq.platform.core.oidc.HQOIDCAuthenticationBackend"
SUBJECT = "example-person"
ROLE_GROUPS = {"admins": "operator", "family": "viewer"}
SIGN_IN = override_settings(
    OIDC_ISSUER=ISSUER,
    OIDC_RP_CLIENT_ID=CLIENT,
    OIDC_RP_SIGN_ALGO="RS256",
    SEVERINO_OIDC_ROLE_GROUPS=ROLE_GROUPS,
    SEVERINO_OIDC_ALLOWED_EMAILS={"sam@example.com": "viewer", "lee@example.com": "operator"},
)


def _backend() -> HQOIDCAuthenticationBackend:
    """A backend partway through a sign-in, as ``authenticate()`` leaves it."""

    backend = HQOIDCAuthenticationBackend()
    backend.request = type("Request", (), {"session": {}})()
    return backend


def _signed_in(claims):
    """The user the backend signs ``claims`` in as, the provider's userinfo answering ``claims``."""

    backend = _backend()
    with patch.object(HQOIDCAuthenticationBackend, "get_userinfo", return_value=claims):
        return backend.get_or_create_user("access-token", "id-token", claims)


def _role(user) -> str:
    user.refresh_from_db()
    return getattr(getattr(user, "person_role", None), "role", "")


@SIGN_IN
class ResolvedRoleTests(TestCase):
    def test_each_group_gives_the_role_its_entry_names(self):
        with no_default_role():
            operator = _signed_in({"sub": "subject-1", "preferred_username": "joe", "groups": ["admins"]})
            viewer = _signed_in({"sub": "subject-2", "preferred_username": "kim", "groups": ["family"]})

        self.assertEqual((_role(operator), _role(viewer)), ("operator", "viewer"))

    def test_the_highest_role_among_a_persons_groups_wins(self):
        with no_default_role():
            user = _signed_in({"sub": "subject-1", "preferred_username": "joe", "groups": ["family", "admins"]})

        self.assertEqual(_role(user), "operator")

    def test_an_unknown_group_or_none_is_refused_and_writes_no_role(self):
        backend = _backend()
        for case, claims in (
            ("an unknown group", {"groups": ["strangers"]}),
            ("no groups claim", {}),
            ("groups that is not a list", {"groups": "admins"}),
        ):
            with self.subTest(case), no_default_role():
                self.assertFalse(backend.verify_claims({"sub": "subject-1", "preferred_username": "joe", **claims}))
                with self.assertRaises(SuspiciousOperation):
                    _signed_in({"sub": "subject-1", "preferred_username": "joe", **claims})
        self.assertFalse(User.objects.exists())

    def test_an_allowed_address_without_a_group_holds_its_entrys_role(self):
        verified = {"email_verified": True, "groups": []}
        with no_default_role():
            sam = _signed_in({"sub": "subject-1", "email": "sam@example.com", **verified})
            lee = _signed_in({"sub": "subject-2", "email": "lee@example.com", **verified})

        self.assertEqual((_role(sam), _role(lee)), ("viewer", "operator"))

    def test_an_unverified_allowed_address_holds_nothing(self):
        self.assertFalse(_backend().verify_claims({"sub": "subject-1", "email": "lee@example.com", "groups": []}))

    def test_a_changed_role_is_audited(self):
        claims = {"sub": "subject-1", "preferred_username": "joe", "groups": ["admins"]}
        with no_default_role():
            user = _signed_in(claims)
        _signed_in({**claims, "groups": ["family"]})

        self.assertEqual(_role(user), "viewer")
        changed = AuditLog.objects.get(object_type=audited_labels()[PersonRole], action=AuditLog.Action.UPDATED)
        # The person is named in the row itself, so it outlives their account.
        self.assertEqual(
            (changed.metadata["changes"]["role"], changed.metadata["actor"]), (["operator", "viewer"], "joe")
        )


class RoleSettingTests(TestCase):
    def test_an_entry_without_a_known_role_stops_hq(self):
        for entries in (["admins"], ["admins=owner"], ["=operator"], ["admins=operator", "admins=viewer"]):
            with self.subTest(entries=entries), self.assertRaises(RuntimeError):
                roles.parse(entries, setting="SEVERINO_OIDC_ROLE_GROUPS")

    def test_an_address_takes_viewer_unless_its_entry_names_a_role(self):
        parsed = roles.parse(["sam@example.com", "lee@example.com=operator"], setting="x", default=roles.Role.VIEWER)

        self.assertEqual(parsed, {"sam@example.com": "viewer", "lee@example.com": "operator"})


class WebPrincipalTests(TestCase):
    def test_a_person_with_no_role_holds_nothing(self):
        with no_default_role():
            user = User.objects.create_user("example-person")

        self.assertEqual(web_principal(user).capabilities, frozenset())

    def test_a_viewer_reads_and_cannot_write(self):
        user = User.objects.create_user("example-person")
        roles.assign(user, roles.Role.VIEWER)
        principal = web_principal(User.objects.get(pk=user.pk))

        self.assertTrue(principal.permits(Capability.READ))
        self.assertFalse(principal.permits(Capability.MANAGE_INFRASTRUCTURE))
        self.assertFalse(principal.permits("write_projects"))

    def test_a_viewers_write_in_the_browser_is_refused(self):
        user = User.objects.create_user("example-person")
        roles.assign(user, roles.Role.VIEWER)
        self.client.force_login(user, backend=BACKEND)

        self.client.post(reverse("theme"), {"theme": "dark"})

        self.assertFalse(hasattr(User.objects.get(pk=user.pk), "appearance"))

    def test_a_new_superuser_operates_hq(self):
        with no_default_role():
            user = User.objects.create_superuser("example-admin")

        self.assertEqual(_role(user), "operator")

    def test_one_request_reads_the_role_at_most_once(self):
        user = User.objects.create_user("example-person")
        self.client.force_login(user, backend=BACKEND)

        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

        reads = [query["sql"] for query in queries if "core_personrole" in query["sql"]]
        self.assertLessEqual(len(reads), 1, reads)


class CallbackMixin:
    """The provider's answer to a sign-in, arriving at HQ's callback."""

    STATE = "example-state"

    def _callback(self, claims, *, id_token=None):
        session = self.client.session
        session["oidc_states"] = {self.STATE: {"nonce": NONCE, "code_verifier": None}}
        session.save()
        with (
            _serving(),
            patch.object(
                HQOIDCAuthenticationBackend,
                "get_token",
                return_value={"id_token": id_token or _id_token(sub=SUBJECT), "access_token": "example-access"},
            ),
            patch.object(HQOIDCAuthenticationBackend, "get_userinfo", return_value=claims),
        ):
            return self.client.get(reverse("oidc_authentication_callback"), {"code": "example", "state": self.STATE})


@SIGN_IN
class RefusalReasonTests(CallbackMixin, TestCase):
    """A sign-in HQ refuses lands on the sign-in page saying why, and the audit holds the same reason."""

    def _shown_reason(self, claims) -> str:
        response = self._callback(claims)
        page = self.client.get(response["Location"])
        denied = AuditLog.objects.filter(object_type="Sign-in", action=AuditLog.Action.DENIED).latest("pk")
        self.assertEqual(page.context["sso_failure"], denied.message)
        self.assertNotIn("_auth_user_id", self.client.session)
        return page.context["sso_failure"]

    def test_a_person_in_no_admitted_group_is_told_so(self):
        reason = self._shown_reason({"sub": SUBJECT, "preferred_username": "example-person", "groups": ["strangers"]})

        # Which groups exist is the operator's to say.
        self.assertNotIn("strangers", reason)

    @override_settings(SEVERINO_OIDC_ROLE_GROUPS={}, SEVERINO_OIDC_ALLOWED_EMAILS={})
    def test_a_host_that_admits_nobody_says_its_settings_name_nobody(self):
        self._shown_reason({"sub": SUBJECT, "preferred_username": "example-person", "groups": ["admins"]})

    def test_an_answer_naming_no_account_says_so(self):
        for case, claims in (
            ("no name or address", {"sub": SUBJECT, "groups": ["admins"]}),
            ("no subject", {"preferred_username": "example-person", "groups": ["admins"]}),
        ):
            with self.subTest(case):
                self._shown_reason(claims)


@SIGN_IN
class RenewalTests(CallbackMixin, TestCase):
    """A renewal is a sign-in while signed in: it re-resolves the role, and a refusal ends the session."""

    def setUp(self):
        self.user = User.objects.create_user("example-person")
        bind_sign_in(self.user, sign_in_subject(ISSUER, SUBJECT))
        self.client.force_login(self.user, backend=BACKEND)

    def _renew(self, *, groups, id_token=None):
        claims = {"sub": SUBJECT, "preferred_username": "example-person", "groups": groups}
        return self._callback(claims, id_token=id_token)

    def _signed_in(self) -> bool:
        return "_auth_user_id" in self.client.session

    def test_a_renewal_in_a_lower_group_keeps_the_session_and_lowers_the_role(self):
        self._renew(groups=["family"])

        self.assertTrue(self._signed_in())
        self.assertEqual(_role(self.user), "viewer")

    def test_a_renewal_without_an_admitted_group_ends_the_session(self):
        response = self._renew(groups=["strangers"])

        self.assertFalse(self._signed_in())
        self.assertEqual(_role(self.user), "operator")
        self.assertIn("sso_failed", response["Location"])

    def test_a_renewal_whose_token_is_refused_ends_the_session_and_says_why(self):
        self._renew(groups=["admins"], id_token=_id_token(sub=SUBJECT, aud="example-other"))

        self.assertFalse(self._signed_in())
        self.assertIn(SSO_FAILURE_SESSION_KEY, self.client.session)

    def test_a_link_to_the_callback_signs_nobody_out(self):
        self.client.get(reverse("oidc_authentication_callback"))

        self.assertTrue(self._signed_in())
