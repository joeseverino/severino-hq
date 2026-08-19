"""Encrypting the few secrets HQ has to hold, and refusing to hold them otherwise.

HQ deliberately keeps provider credentials out of the web container -- they are
rendered to a root-owned file the controller reads and HQ cannot. That covers
every credential HQ *uses*.

It does not cover a secret an operator hands to HQ on purpose. An internally
signed certificate is generated on an air-gapped machine and has to reach Nginx
Proxy Manager somehow; today that is a copy and paste into a web form, and doing
it through HQ is the same key crossing the same wire. Keeping it afterwards is
what makes "install this on another service too" a click rather than a trip back
to the offline CA.

So: encrypted at rest, with a key that is not in the database. If the key is not
configured, storing fails loudly. A secret store that silently degrades to
plaintext is worse than not having one, because the operator believes the first
sentence of this docstring.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


class SecretsUnavailable(RuntimeError):
    """No key material is configured, so nothing can be stored or read."""


def available() -> bool:
    return bool(_configured_key())


def _configured_key() -> str:
    return (getattr(settings, "SEVERINO_SECRET_STORE_KEY", "") or "").strip()


def _cipher() -> Fernet:
    key = _configured_key()
    if len(key) < 32:
        raise SecretsUnavailable(
            "SEVERINO_SECRET_STORE_KEY must be set to at least 32 characters "
            "before HQ can hold a secret. Nothing was stored."
        )
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
        raise SecretsUnavailable(
            "A stored secret could not be decrypted with the configured key."
        ) from exc
