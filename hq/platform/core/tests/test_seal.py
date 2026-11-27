"""What HQ seals to the controller opens only in the controller, for the place it was sealed for.

The controller's half is controller/runtime/seal_test.go, which opens the
vector these tests share, sealed by this module.
"""

import base64
import hashlib
import secrets as random

from cryptography.hazmat.primitives import hpke, serialization
from cryptography.hazmat.primitives.asymmetric import mlkem, x25519
from django.test import SimpleTestCase, override_settings

from hq.platform.core import secrets

SUITE = hpke.Suite(hpke.KEM.MLKEM768_X25519, hpke.KDF.HKDF_SHA256, hpke.AEAD.AES_256_GCM)


def private_key(seed: bytes) -> hpke.MLKEM768X25519PrivateKey:
    """The controller's private key from its seed, as Go's crypto/hpke expands
    one: SHAKE256 gives the ML-KEM-768 seed, then the X25519 key."""

    expanded = hashlib.shake_256(seed).digest(96)
    return hpke.MLKEM768X25519PrivateKey(
        mlkem.MLKEM768PrivateKey.from_seed_bytes(expanded[:64]),
        x25519.X25519PrivateKey.from_private_bytes(expanded[64:]),
    )


def public_bytes(seed: bytes) -> bytes:
    expanded = hashlib.shake_256(seed).digest(96)
    pq = mlkem.MLKEM768PrivateKey.from_seed_bytes(expanded[:64]).public_key().public_bytes_raw()
    curve = x25519.X25519PrivateKey.from_private_bytes(expanded[64:]).public_key()
    return pq + curve.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def ciphertext(sealed: str) -> bytes:
    body = sealed.split(".", 2)[2]
    return base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))


def a_public_key() -> str:
    return base64.b64encode(public_bytes(random.token_bytes(32))).decode()


class FailClosedTests(SimpleTestCase):
    """Without the controller's key, nothing is sealed, and no other key is used."""

    def test_no_key_or_a_malformed_one_seals_nothing(self):
        for value in ("", "not base64!", base64.b64encode(bytes(1215)).decode()):
            with self.subTest(value=value[:12]), override_settings(SEVERINO_SEAL_PUBLIC_KEY=value):
                self.assertFalse(secrets.sealing_available())
                with self.assertRaises(secrets.SecretsUnavailable):
                    secrets.seal_for_controller("an example secret", resource_key="example.service", slot="env:A")

    def test_a_place_that_could_be_read_two_ways_is_refused(self):
        with override_settings(SEVERINO_SEAL_PUBLIC_KEY=a_public_key()):
            for resource_key, slot in (("example|service", "env:A"), ("example.service", "env:A|B"), ("", "env:A")):
                with self.subTest(resource_key=resource_key, slot=slot), self.assertRaises(ValueError):
                    secrets.seal_for_controller("x", resource_key=resource_key, slot=slot)
