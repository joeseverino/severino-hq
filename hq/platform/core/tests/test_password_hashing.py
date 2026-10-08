"""Password storage: Argon2 for every new hash, older formats upgraded on use.

Argon2 is a native extension. The container gate runs this file inside the
built image, which is where a missing or unbuildable wheel would show.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import (
    PBKDF2PasswordHasher,
    check_password,
    get_hasher,
    identify_hasher,
    make_password,
)
from django.test import SimpleTestCase, TestCase

PASSWORD = "correct-horse-battery"


class DefaultHasherTests(SimpleTestCase):
    def test_argon2_is_the_default(self):
        self.assertEqual(
            settings.PASSWORD_HASHERS[0],
            "hq.platform.core.hashers.Argon2PasswordHasher",
        )
        self.assertEqual(get_hasher("default").algorithm, "argon2")

    def test_the_native_library_hashes_and_verifies(self):
        encoded = make_password(PASSWORD)
        self.assertEqual(identify_hasher(encoded).algorithm, "argon2")
        self.assertTrue(check_password(PASSWORD, encoded))

    def test_a_wrong_password_does_not_verify(self):
        self.assertFalse(check_password("wrong", make_password(PASSWORD)))

    def test_a_tampered_or_unknown_hash_does_not_verify(self):
        encoded = make_password(PASSWORD)
        digest = encoded.rsplit("$", 1)[1]
        tampered = encoded[: -len(digest)] + ("A" if digest[0] != "A" else "B") + digest[1:]
        self.assertFalse(check_password(PASSWORD, tampered))
        self.assertFalse(check_password(PASSWORD, "unknown$1$salt$hash"))
        self.assertFalse(check_password(PASSWORD, make_password(None)))

    def test_older_formats_still_verify(self):
        names = [path.rsplit(".", 1)[1] for path in settings.PASSWORD_HASHERS]
        self.assertIn("PBKDF2PasswordHasher", names[1:])
        self.assertIn("PBKDF2SHA1PasswordHasher", names[1:])


class UpgradeOnSignInTests(TestCase):
    """A stored PBKDF2 hash signs in and is rewritten as Argon2."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="joe")
        self.user.password = PBKDF2PasswordHasher().encode(PASSWORD, "examplesalt")
        self.user.save(update_fields=["password"])

    def _sign_in(self, password):
        return self.client.post(
            "/accounts/login/",
            {"username": "joe", "password": password},
            REMOTE_ADDR="100.64.0.1",
        )

    def test_an_old_hash_signs_in_and_is_upgraded(self):
        response = self._sign_in(PASSWORD)
        self.assertEqual(response.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)
        self.user.refresh_from_db()
        self.assertEqual(identify_hasher(self.user.password).algorithm, "argon2")
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_a_corrupt_argon2_hash_refuses_rather_than_failing(self):
        for corrupt in ("argon2$garbage", "argon2$argon2id$v=19$m=102400,t=2,p=8$bad"):
            with self.subTest(corrupt=corrupt):
                self.user.password = corrupt
                self.user.save(update_fields=["password"])
                self.assertFalse(check_password(PASSWORD, corrupt))
                response = self._sign_in(PASSWORD)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_failed_sign_in_leaves_the_old_hash_alone(self):
        before = self.user.password
        response = self._sign_in("wrong")
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertNotEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.password, before)
