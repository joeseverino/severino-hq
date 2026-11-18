"""A page's own markup apart from the examples it draws in shadow roots.

The Design page draws each shared part in a declarative shadow root
(``<template shadowrootmode>``), a tree of its own: its regions, filters and
forms are the example's, and no part of the page's. The build follows and
saves only what the page itself offers. No Django here, since ``check.py``
imports none.
"""

import re

_TEMPLATE = re.compile(r"<template\b([^>]*)>|</template\s*>", re.IGNORECASE)


def own(page: str) -> str:
    """``page`` without the examples it draws in shadow roots."""

    kept: list[str] = []
    at = 0
    depth = 0
    for tag in _TEMPLATE.finditer(page):
        opening = tag.group(0)[1] != "/"
        if not depth:
            if opening and "shadowrootmode" in tag.group(1).lower():
                kept.append(page[at : tag.start()])
                depth = 1
            continue
        depth += 1 if opening else -1
        if not depth:
            at = tag.end()
    if not depth:
        kept.append(page[at:])
    return "".join(kept)
