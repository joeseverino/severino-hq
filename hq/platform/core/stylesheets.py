"""HQ's stylesheet: the sheets every page links, in cascade order.

The sheets share the cascade layers the first one declares (`tokens.css`).
Inside a layer a later rule wins a tie of specificity, and a rule in a later
sheet is a later rule, so this order is part of the cascade: a sheet moves
only when its rules may come later than the ones they used to follow. Each
sheet is one responsibility, held under the source-file ceiling
(`FileSizeTests`).

Several links rather than one sheet that imports the rest: every link is
render-blocking, so the first paint still waits for the whole stylesheet and
nothing is drawn unstyled, and the browser's preload scanner finds every link
in the head at once, where an `@import` is found only once its parent has
arrived. Anything that reads the stylesheet reads it through here.
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
