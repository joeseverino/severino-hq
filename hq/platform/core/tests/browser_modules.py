"""The browser gates' modules, for the guards that read every one of them."""

from pathlib import Path

PACKAGE = Path(__file__).resolve().with_name("browser")
DOTTED = "hq.platform.core.tests.browser"


def browser_modules() -> dict[str, Path]:
    """Each module of the browser package, by its dotted name: a package
    below it is one too, by its ``__init__``, which holds code as a module
    does (the catalog's gate, the probes' ``Check``)."""

    found = {
        ".".join((DOTTED, *path.relative_to(PACKAGE).with_suffix("").parts)).removesuffix(".__init__"): path
        for path in sorted(PACKAGE.rglob("*.py"))
        if path.parent != PACKAGE or path.name != "__init__.py"
    }
    assert found, f"no browser module under {PACKAGE}"
    return found
