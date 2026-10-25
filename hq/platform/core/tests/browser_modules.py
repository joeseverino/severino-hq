"""The browser gates' modules, for the guards that read every one of them."""

from pathlib import Path

PACKAGE = Path(__file__).resolve().with_name("browser")
DOTTED = "hq.platform.core.tests.browser"


def browser_modules() -> dict[str, Path]:
    """Each module of the browser package, by its dotted name."""

    found = {
        ".".join((DOTTED, *path.relative_to(PACKAGE).with_suffix("").parts)): path
        for path in sorted(PACKAGE.rglob("*.py"))
        if path.name != "__init__.py"
    }
    assert found, f"no browser module under {PACKAGE}"
    return found
