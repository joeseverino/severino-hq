"""One text and one digest for a JSON value, so two equal values always
share them: keys sorted, no spaces, a value JSON has no form for written as
its ``str``.

Approval and idempotency fingerprints are stored and compared with what is
asked later, so these bytes never change.
"""

import hashlib
import json
from typing import Any


def canonical(value: Any, *, ensure_ascii: bool = True) -> str:
    """``value`` as the one JSON text every equal value shares; with
    ``ensure_ascii`` false, text past ASCII as itself rather than escaped,
    for a fingerprint first stored that way."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=ensure_ascii)


def digest(value: Any, *, ensure_ascii: bool = True) -> str:
    """The SHA-256 of ``value``'s canonical text, in hex."""

    return hashlib.sha256(canonical(value, ensure_ascii=ensure_ascii).encode()).hexdigest()
