"""JSON read as HQ's doors read a request: a repeated field or a bare NaN or Infinity is refused,
since Python's parser would keep the last copy or a value no other parser agrees on."""

import json
from typing import Any


def _no_constant(_value: str) -> Any:
    raise ValueError("Invalid JSON constant")


def _once_each(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field")
        value[key] = item
    return value


def loads(text: str | bytes) -> Any:
    """The value ``text`` holds, or ValueError for JSON a strict parser would refuse."""

    return json.loads(text, parse_constant=_no_constant, object_pairs_hook=_once_each)
