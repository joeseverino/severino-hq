"""HQ's stylesheet: the sheets every page links, in cascade order (docs/Design.md).

Several links rather than one sheet importing the rest: each is render-blocking,
so nothing is drawn unstyled, and the preload scanner finds every link in the
head at once, where an `@import` is found only once its parent arrives.
Anything that reads the stylesheet reads it through here.
"""

from pathlib import Path

STATIC = Path(__file__).resolve().parents[3] / "static"
DIRECTORY = "css/app"
SHEETS: tuple[str, ...] = tuple(
    f"{DIRECTORY}/{name}.css"
    for name in (
        "tokens",
        "base",
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
)


def sheet_paths() -> tuple[Path, ...]:
    """Each sheet's source file, in cascade order."""

    return tuple(STATIC / sheet for sheet in SHEETS)


def stylesheet() -> str:
    """The whole stylesheet's source, its sheets in cascade order."""

    return "\n".join(path.read_text(encoding="utf-8") for path in sheet_paths())
