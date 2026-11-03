"""What HQ seals to the controller opens only in the controller, for the place it was sealed for.

The controller's half is controller/runtime/seal_test.go, which opens the
vector these tests share, sealed by this module.
"""

import base64
import hashlib
import json
import secrets as random
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hpke, serialization
from cryptography.hazmat.primitives.asymmetric import mlkem, x25519
from django.test import SimpleTestCase, override_settings, tag

from hq.platform.core import secrets

VECTOR = Path(__file__).resolve().parents[4] / "controller" / "runtime" / "testdata" / "seal"
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


@tag("source")
class TheVectorTests(SimpleTestCase):
    """HQ's sealing against the committed seed, whose public key hq-host would write."""

    def setUp(self):
        self.vector = json.loads((VECTOR / "vector.json").read_text(encoding="utf-8"))
        (seed_file,) = VECTOR.glob("*.seed")
        self.seed = seed_file.read_bytes()
        self.public_key = (VECTOR / "public.key").read_text(encoding="utf-8")

    def seal(self, resource_key: str, slot: str) -> str:
        with override_settings(SEVERINO_SEAL_PUBLIC_KEY=self.public_key):
            return secrets.seal_for_controller(self.vector["plaintext"], resource_key=resource_key, slot=slot)

    def test_the_committed_public_key_is_the_seeds(self):
        self.assertEqual(base64.b64decode(self.public_key), public_bytes(self.seed))

    def test_what_hq_seals_the_seed_opens(self):
        sealed = self.seal(self.vector["resource_key"], self.vector["slot"])
        key_id = hashlib.sha256(public_bytes(self.seed)).hexdigest()[:8]
        self.assertTrue(sealed.startswith(f"hq2.{key_id}."))
        opened = SUITE.decrypt(ciphertext(sealed), private_key(self.seed), self.vector["info"].encode())
        self.assertEqual(opened.decode(), self.vector["plaintext"])
        # Sealing is randomized, so no two sealed values of one secret match.
        self.assertNotEqual(sealed, self.seal(self.vector["resource_key"], self.vector["slot"]))

    def test_a_value_sealed_for_another_place_does_not_open_here(self):
        for resource_key, slot in (
            (self.vector["resource_key"], "env:EXAMPLE_TOKEN"),
            ("example.other", self.vector["slot"]),
        ):
            with self.subTest(resource_key=resource_key, slot=slot):
                sealed = self.seal(resource_key, slot)
                with self.assertRaises(InvalidTag):
                    SUITE.decrypt(ciphertext(sealed), private_key(self.seed), self.vector["info"].encode())


def a_public_key() -> str:
    return base64.b64encode(public_bytes(random.token_bytes(32))).decode()


class FailClosedTests(SimpleTestCase):
    """Without the controller's key, nothing is sealed, and no other key is used."""

    def test_no_key_or_a_malformed_one_seals_nothing(self):
        for value in ("", "not base64!", base64.b64encode(bytes(1215)).decode()):
            with self.subTest(value=value[:12]), override_settings(SEVERINO_SEAL_PUBLIC_KEY=value):
                self.assertFalse(secrets.sealing_available())
                with self.assertRaisesMessage(secrets.SecretsUnavailable, secrets.NO_SEAL_KEY):
                    secrets.seal_for_controller("an example secret", resource_key="example.service", slot="env:A")

    def test_a_key_seals(self):
        with override_settings(SEVERINO_SEAL_PUBLIC_KEY=a_public_key()):
            self.assertTrue(secrets.sealing_available())
            self.assertTrue(
                secrets.seal_for_controller("x", resource_key="example.service", slot="env:A").startswith("hq2.")
            )

    def test_a_place_that_could_be_read_two_ways_is_refused(self):
        with override_settings(SEVERINO_SEAL_PUBLIC_KEY=a_public_key()):
            for resource_key, slot in (("example|service", "env:A"), ("example.service", "env:A|B"), ("", "env:A")):
                with self.subTest(resource_key=resource_key, slot=slot), self.assertRaises(ValueError):
                    secrets.seal_for_controller("x", resource_key=resource_key, slot=slot)
