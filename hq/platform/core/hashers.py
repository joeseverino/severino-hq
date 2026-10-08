"""Password hashers HQ uses, where Django's defaults need a stricter edge."""

from typing import override

from argon2.exceptions import InvalidHashError
from django.contrib.auth import hashers


class Argon2PasswordHasher(hashers.Argon2PasswordHasher):
    """Django's Argon2 hasher, refusing an unreadable stored hash.

    A corrupt ``argon2$`` value raises inside the library; unhandled, sign-in
    answers 500. It is a password that does not match.
    """

    @override
    def verify(self, password: str, encoded: str) -> bool:
        try:
            return super().verify(password, encoded)
        except InvalidHashError:
            return False

    @override
    def must_update(self, encoded: str) -> bool:
        # Asked whether or not the password matched, so it decodes too.
        try:
            return super().must_update(encoded)
        except InvalidHashError:
            return False
