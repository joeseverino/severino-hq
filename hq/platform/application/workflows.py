"""A claim's stable identity, and an action as an answer carries it."""

from hashlib import sha256
from typing import Annotated, Any

from .json_schemas import Written
from .workflow_contracts import ActionLink

# An action as an answer carries it.
WrittenAction = Annotated[dict[str, Any], Written(ActionLink)]


def claim_identity(namespace: str, key: str) -> str:
    """Stable identity for any domain claim across repeated derivations: the
    hash of the one key its source knows it by, so two claims a source tells
    apart never share an id."""

    digest = sha256(f"{namespace}\0{key}".encode()).hexdigest()[:16]
    return f"claim:{digest}"
