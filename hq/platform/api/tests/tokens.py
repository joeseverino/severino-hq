"""Access tokens as the identity provider signs them, and its published key, for a test of a token's door."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from hq.platform.application import tokens

ISSUER = "https://sso.example.test"
RESOURCE = "https://hq.example.test/api"
KID = "example-key"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _token(client: str = "example-automation", *, key: rsa.RSAPrivateKey = _KEY, **overrides: Any) -> str:
    """An access token ``client`` holds as itself; a person's delegated grant names them as ``sub``."""

    if "client_id" in overrides:
        raise TypeError("Name the client as _token's first argument, which is its sub too.")
    now = datetime.now(tz=UTC)
    claims = {
        "iss": ISSUER,
        "aud": RESOURCE,
        "sub": client,
        "client_id": client,
        "scope": "example.write",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": KID, "typ": "at+jwt"})


def _held(key: rsa.RSAPrivateKey) -> dict[str, jwt.PyJWK]:
    jwk = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return {KID: jwt.PyJWK({**jwk, "kid": KID, "use": "sig", "alg": "RS256"})}


def _serving(key: rsa.RSAPrivateKey = _KEY) -> Any:
    """The identity provider's published key, held as the key thread holds it."""

    return patch.object(tokens.KEYS, "held", _held(key))
