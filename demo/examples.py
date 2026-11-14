"""A page's own markup apart from the examples it draws in shadow roots.

The Design page draws each shared part in a declarative shadow root
(``<template shadowrootmode>``), a tree of its own: its links, regions,
filters and forms are the example's, leading where they lead in HQ (nowhere,
for the synthetic ``/example/`` addresses), and no part of the page's. The
build follows and saves only what the page itself offers, and the checks
hold only the page's own links to being in the demo. No Django here, since
``check.py`` imports none.
"""

import re

_TEMPLATE = re.compile(r"<template\b([^>]*)>|</template\s*>", re.IGNORECASE)


def split(page: str) -> tuple[str, str]:
    """``page``'s own markup and the markup of the shadow roots it holds."""

    own: list[str] = []
    examples: list[str] = []
    at = 0
    depth = 0
    for tag in _TEMPLATE.finditer(page):
        opening = tag.group(0)[1] != "/"
        if not depth:
            if opening and "shadowrootmode" in tag.group(1).lower():
                own.append(page[at : tag.start()])
                at, depth = tag.start(), 1
            continue
        depth += 1 if opening else -1
        if not depth:
            examples.append(page[at : tag.end()])
            at = tag.end()
    (examples if depth else own).append(page[at:])
    return "".join(own), "".join(examples)


def own(page: str) -> str:
    """``page`` without the examples it draws in shadow roots."""

    return split(page)[0]
