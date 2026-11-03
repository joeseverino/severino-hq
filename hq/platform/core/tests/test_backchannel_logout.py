"""The identity provider's back-channel logout ends every HQ session of the person it names, and nothing else.

The tokens take their shape from Pocket ID's (backend/internal/backchannellogout/logout_token.go):
`typ` logout+jwt, `aud` a list naming the client, `sub`, `iat`, `exp`, `jti`
and the back-channel logout event, and no `sid`.
"""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from hq.platform.api.tests.tokens import _KEY, ISSUER, KID, OTHER_KEY, _serving
from hq.platform.application.linked_accounts import bind_sign_in, sign_in_subject
from hq.platform.core.models import AuditLog

User = get_user_model()
CLIENT = "example-hq"
SUBJECT = "example-person"
EVENT = "http://schemas.openid.net/event/backchannel-logout"


def _logout_token(key=_KEY, typ="logout+jwt", **overrides) -> str:
    now = datetime.now(tz=UTC)
    claims = {
        "iss": ISSUER,
        "aud": [CLIENT],
        "sub": SUBJECT,
        "iat": now,
        "exp": now + timedelta(minutes=2),
        "jti": str(uuid.uuid4()),
        "events": {EVENT: {}},
    }
    claims.update(overrides)
    claims = {name: value for name, value in claims.items() if value is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": KID, "typ": typ})


@override_settings(OIDC_ISSUER=ISSUER, OIDC_RP_CLIENT_ID=CLIENT, OIDC_RP_SIGN_ALGO="RS256")
class BackchannelLogoutTests(TestCase):
    def setUp(self):
        self.person = User.objects.create_user("example-person")
        bind_sign_in(self.person, sign_in_subject(ISSUER, SUBJECT))
        self.other = User.objects.create_user("example-other")
        self.sessions = [self._signed_in(self.person), self._signed_in(self.person)]
        self.others = self._signed_in(self.other)

    def _signed_in(self, user) -> str:
        client = Client()
        client.force_login(user)
        return client.session.session_key

    def _post(self, token):
        with _serving():
            # As the provider's server sends it: a form post, with no session and no CSRF token.
            return Client(enforce_csrf_checks=True).post(reverse("oidc_backchannel_logout"), {"logout_token": token})

    def _live(self, key) -> bool:
        return SessionStore().exists(key)

    def test_a_valid_token_ends_every_session_of_its_person_and_no_other(self):
        response = self._post(_logout_token())

        self.assertEqual(response.status_code, 200)
        self.assertEqual([self._live(key) for key in self.sessions], [False, False])
        self.assertTrue(self._live(self.others))
        ended = AuditLog.objects.get(action=AuditLog.Action.LOGOUT, object_id=str(self.person.pk))
        self.assertEqual(ended.metadata["sessions"], 2)

    def test_a_redelivered_token_acts_once(self):
        token = _logout_token()
        first = self._post(token)
        self.sessions.append(self._signed_in(self.person))
        again = self._post(token)

        self.assertEqual([first.status_code, again.status_code], [200, 200])
        # The first delivery ended both sessions; the second ends nothing, not even a later sign-in.
        self.assertEqual([self._live(key) for key in self.sessions], [False, False, True])
        self.assertEqual(
            AuditLog.objects.filter(action=AuditLog.Action.LOGOUT, object_id=str(self.person.pk)).count(), 1
        )

    def test_a_person_hq_does_not_know_is_done(self):
        response = self._post(_logout_token(sub="example-stranger"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(self._live(key) for key in [*self.sessions, self.others]))
        self.assertFalse(AuditLog.objects.filter(action=AuditLog.Action.LOGOUT).exists())

    def test_every_check_refuses_its_token_and_ends_nothing(self):
        past = datetime.now(tz=UTC) - timedelta(minutes=5)
        for case, token in (
            ("an ID token", _logout_token(typ="JWT")),
            ("an access token", _logout_token(typ="at+jwt")),
            ("another client", _logout_token(aud=["example-other"])),
            ("another issuer", _logout_token(iss="https://evil.example.test")),
            ("no events", _logout_token(events=None)),
            ("another event", _logout_token(events={"https://example.com/other-event": {}})),
            ("a nonce", _logout_token(nonce="example-nonce")),
            ("no subject", _logout_token(sub=None)),
            ("an empty subject", _logout_token(sub=" ")),
            ("no jti", _logout_token(jti=None)),
            ("expired", _logout_token(iat=past - timedelta(minutes=2), exp=past)),
            ("another key", _logout_token(key=OTHER_KEY)),
            ("not a token", "example-not-a-token"),
            ("nothing", ""),
        ):
            with self.subTest(case):
                self.assertEqual(self._post(token).status_code, 400)
        self.assertTrue(all(self._live(key) for key in [*self.sessions, self.others]))

    def test_it_answers_only_a_post(self):
        with _serving():
            response = Client().get(reverse("oidc_backchannel_logout"), {"logout_token": _logout_token()})

        self.assertEqual(response.status_code, 405)
        self.assertTrue(all(self._live(key) for key in self.sessions))
