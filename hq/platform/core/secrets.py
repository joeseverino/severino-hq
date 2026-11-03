"""Sealing the few secrets an operator hands HQ on purpose to the controller, and refusing to hold them otherwise.

The credentials HQ uses never reach the web container. A secret handed to it (an
internally signed certificate's key, kept so installing it on another service is
a click) is sealed to the controller: HQ holds only the public half of the
controller's key, so what it seals only the controller can open. With no key,
storing fails loudly, since a store that degrades to plaintext is worse than
none. controller/internal/sealing/sealing.go owns the format, and every
constant below agrees with it.
"""

import base64
import binascii
import hashlib

from cryptography.hazmat.primitives import hpke
from cryptography.hazmat.primitives.asymmetric import mlkem, x25519
from django.conf import settings

from .errors import UpstreamUnavailable


class SecretsUnavailable(UpstreamUnavailable):
    """No key material is configured, so nothing can be sealed."""


# What sealing says without the controller's public key.
NO_SEAL_KEY = "HQ has not been given the controller's sealing key, so it cannot hold a secret. Nothing was stored."

# HPKE (RFC 9180): ML-KEM-768+X25519, HKDF-SHA256, AES-256-GCM.
_SUITE = hpke.Suite(hpke.KEM.MLKEM768_X25519, hpke.KDF.HKDF_SHA256, hpke.AEAD.AES_256_GCM)
# The public key is ML-KEM-768's encapsulation key, then X25519's.
_MLKEM768_PUBLIC_BYTES = 1184
_PUBLIC_KEY_BYTES = _MLKEM768_PUBLIC_BYTES + 32
SEALED_PREFIX = "hq2"
_INFO_VERSION = "hq-seal/v1"


def sealing_available() -> bool:
    try:
        _controller_key()
    except SecretsUnavailable:
        return False
    return True


def _controller_key() -> tuple[str, hpke.MLKEM768X25519PublicKey]:
    """The controller's public key and the id a sealed value names it by."""

    try:
        public = base64.b64decode(settings.SEVERINO_SEAL_PUBLIC_KEY.strip(), validate=True)
    except binascii.Error as exc:
        raise SecretsUnavailable(NO_SEAL_KEY) from exc
    if len(public) != _PUBLIC_KEY_BYTES:
        raise SecretsUnavailable(NO_SEAL_KEY)
    try:
        key = hpke.MLKEM768X25519PublicKey(
            mlkem.MLKEM768PublicKey.from_public_bytes(public[:_MLKEM768_PUBLIC_BYTES]),
            x25519.X25519PublicKey.from_public_bytes(public[_MLKEM768_PUBLIC_BYTES:]),
        )
    except ValueError as exc:
        raise SecretsUnavailable(NO_SEAL_KEY) from exc
    return hashlib.sha256(public).hexdigest()[:8], key


def _info(resource_key: str, slot: str) -> bytes:
    """What binds a sealed value to its place, since HPKE's single-shot API
    takes no associated data. Neither part may hold the separator, so no two
    places share one."""

    for part in (resource_key, slot):
        if not part or "|" in part:
            raise ValueError("A sealed value is bound to a key and a slot, each non-empty and without '|'.")
    return f"{_INFO_VERSION}|{resource_key}|{slot}".encode()


def seal_for_controller(plaintext: str, *, resource_key: str, slot: str) -> str:
    """plaintext sealed to the controller, opening only for this resource and slot."""

    key_id, key = _controller_key()
    ciphertext = _SUITE.encrypt(plaintext.encode(), key, _info(resource_key, slot))
    body = base64.urlsafe_b64encode(ciphertext).rstrip(b"=").decode()
    return f"{SEALED_PREFIX}.{key_id}.{body}"
