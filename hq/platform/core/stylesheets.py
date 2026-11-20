"""HQ's stylesheet: the sheets every page links, in cascade order (docs/Design.md).

Several links rather than one sheet importing the rest: each is render-blocking,
so nothing is drawn unstyled, and the preload scanner finds every link in the
head at once, where an `@import` is found only once its parent arrives.
Anything that reads the stylesheet reads it through here.
"""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[3] / "static"
DIRECTORY = "css/app"
# The cascade order of the sheets it names; one it leaves out is linked after them.
ORDER = (
    "tokens",
    "base",
    "cuts",
    "page",
    "figures",
    "tables",
    "cards",
    "layout",
    "forms",
    "routes",
    "cadence",
    "connection",
    "pages",
    "watching",
    "surfaces",
)
# Every sheet in the directory, so none is left unlinked.
SHEETS: tuple[str, ...] = tuple(
    f"{DIRECTORY}/{name}.css"
    for name in sorted(
        (path.stem for path in (STATIC / DIRECTORY).glob("*.css")),
        key=lambda name: (ORDER.index(name) if name in ORDER else len(ORDER), name),
    )
)


def sheet_paths() -> tuple[Path, ...]:
    """Each sheet's source file, in cascade order."""

    return tuple(STATIC / sheet for sheet in SHEETS)


def stylesheet() -> str:
    """The whole stylesheet's source, its sheets in cascade order."""

    return "\n".join(path.read_text(encoding="utf-8") for path in sheet_paths())


# Each face's file, as `{% static %}` names it. A face is shown only if it is
# ready for the first paint (`font-display: optional`, which lint:css holds), so
# the head preloads each beside the stylesheet, where it usually is ready.
# lint:css holds every url() to its quotes, the form read here.
FACES: tuple[str, ...] = tuple(
    (path.parent / source).resolve().relative_to(STATIC).as_posix()
    for path in sheet_paths()
    for source in re.findall(r'@font-face\s*\{[^}]*\burl\("([^"]+)"\)', path.read_text(encoding="utf-8"))
)
