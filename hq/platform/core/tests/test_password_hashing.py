"""Password storage: Argon2 is the only hasher.

The suite stores with a fast hasher first (settings.py says why), so these
tests hold production's list as a process outside the suite loads it, and
what HQ's Argon2 class adds to Django's. That the native library hashes in
the built image is image:verify:packages' to show.
"""

import json
import subprocess
import sys

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.test import SimpleTestCase, TestCase

from hq.platform.core.hashers import Argon2PasswordHasher

PASSWORD = "correct-horse-battery"
CORRUPT = ("argon2$garbage", "argon2$argon2id$v=19$m=102400,t=2,p=8$bad")


class ProductionHasherTests(SimpleTestCase):
    def test_production_stores_with_argon2_alone(self):
        # Its own process, whose argv is not `manage.py test`: settings as the
        # running host loads them.
        loaded = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import json, django; from django.conf import settings; django.setup(); "
                    "print(json.dumps(settings.PASSWORD_HASHERS))"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(loaded.returncode, 0, loaded.stderr[-4000:])
        self.assertEqual(json.loads(loaded.stdout), ["hq.platform.core.hashers.Argon2PasswordHasher"])


class Argon2HasherTests(SimpleTestCase):
    hasher = Argon2PasswordHasher()

    def test_a_corrupt_hash_is_a_password_that_does_not_match(self):
        for corrupt in CORRUPT:
            with self.subTest(corrupt=corrupt):
                self.assertFalse(self.hasher.verify(PASSWORD, corrupt))
                self.assertFalse(self.hasher.must_update(corrupt))


class SignInTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="joe")
        self.user.password = make_password(PASSWORD, hasher="argon2")
        self.user.save(update_fields=["password"])

    def _sign_in(self, password):
        return self.client.post(
            "/accounts/login/",
            {"username": "joe", "password": password},
            REMOTE_ADDR="100.64.0.1",
        )

    def test_a_corrupt_argon2_hash_refuses_rather_than_failing(self):
        for corrupt in CORRUPT:
            with self.subTest(corrupt=corrupt):
                self.user.password = corrupt
                self.user.save(update_fields=["password"])
                self.assertFalse(check_password(PASSWORD, corrupt))
                response = self._sign_in(PASSWORD)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_failed_sign_in_leaves_the_hash_alone(self):
        before = self.user.password
        response = self._sign_in("wrong")
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertNotEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.password, before)
