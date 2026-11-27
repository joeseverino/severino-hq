"""What HQ's Argon2 hasher adds to Django's: a corrupt hash is a password that does not match."""

from django.test import SimpleTestCase

from hq.platform.core.hashers import Argon2PasswordHasher

PASSWORD = "correct-horse-battery"
CORRUPT = ("argon2$garbage", "argon2$argon2id$v=19$m=102400,t=2,p=8$bad")


class Argon2HasherTests(SimpleTestCase):
    hasher = Argon2PasswordHasher()

    def test_a_corrupt_hash_is_a_password_that_does_not_match(self):
        for corrupt in CORRUPT:
            with self.subTest(corrupt=corrupt):
                self.assertFalse(self.hasher.verify(PASSWORD, corrupt))
                self.assertFalse(self.hasher.must_update(corrupt))
