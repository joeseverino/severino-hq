"""A template's comments, as the readers of its text skip them."""

import re
from pathlib import Path

lazy from hq.platform.core.hand_drawn import OPEN_COMMENT, Source

TEMPLATE_COMMENT = re.compile(r"\{#.*?#\}|\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}", re.DOTALL)


def unclosed_template_comments(template_root: str | Path) -> list[str]:
    """Every comment left open under ``template_root``, as "page.html:line":
    the open-comment shape, for an extension's own suite."""

    root = Path(template_root)
    found = []
    for path in sorted(root.rglob("*.html")):
        read = Source(path.read_text(encoding="utf-8", errors="replace"))
        found.extend(
            f"{path.relative_to(root)}:{read.line(match.start())}" for match in OPEN_COMMENT.finditer(read.text)
        )
    return found
