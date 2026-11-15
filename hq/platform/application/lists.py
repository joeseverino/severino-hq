"""How HQ reads a comma-separated list someone wrote: a setting, a tag field, a header.

One reading, so a stray space or a trailing comma means the same everywhere.
A caller that must refuse an empty item, or keep its spacing, splits on its own.
"""


def split_list(text: str) -> list[str]:
    """The items of ``text``, each stripped, with the empty ones left out."""

    return [item for item in (piece.strip() for piece in text.split(",")) if item]
