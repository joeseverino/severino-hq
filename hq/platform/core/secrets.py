"""Encrypting the few secrets an operator hands HQ on purpose, and refusing to hold them otherwise.

The credentials HQ uses never reach the web container. A secret handed to it (an
internally signed certificate's key, kept so installing it on another service is
a click) is encrypted at rest with a key that is not in the database; with no
key, storing fails loudly, since a store that degrades to plaintext is worse
than none.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

from .errors import UpstreamUnavailable


class SecretsUnavailable(UpstreamUnavailable):
    """No key material is configured, so nothing can be stored or read."""


# What sealing says without a key, and what a use case that seals refuses with.
NO_STORE_KEY = (
    "HQ's settings need a secret store key of at least 32 characters before HQ can hold a secret. Nothing was stored."
)


def available() -> bool:
    return bool(_configured_key())


def _configured_key() -> str:
    return settings.SEVERINO_SECRET_STORE_KEY.strip()


def _cipher() -> Fernet:
    key = _configured_key()
    if len(key) < 32:
        raise SecretsUnavailable(NO_STORE_KEY)
    # Derived rather than required to be a Fernet key, so the value in 1Password
    # is an ordinary long secret like every other entry rather than something
    # with a format the operator has to generate correctly.
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))


def seal(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _cipher().encrypt(plaintext.encode()).decode()


def unseal(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _cipher().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        # Almost always a rotated or mistyped key rather than tampering, and
        # either way the stored value is unreadable and saying so beats
        # returning something that looks like an empty secret.
        raise SecretsUnavailable("A stored secret could not be decrypted with the configured key.") from exc
