"""The page-class check's reading of the host: the classes each template
writes beyond those it may, read from the parts that own them.

A support module rather than part of the check, so ``mise run test --at``
holds a change to the reader to the check at a ref.
"""

import inspect

from hq.platform.core.stylesheets import STATIC
from hq.platform.core.table_cells import ROLE_CLASSES
from hq.platform.core.template_contract import class_words, parts, public_classes

# The host's own templates, which no extension's check reads.
TEMPLATES = STATIC.parent / "templates"


def hand_rolled_classes() -> set[tuple[str, str]]:
    """Each class a host template writes that it may not, with the template's
    name.

    Any template may write the public classes (``public_classes``) and a
    table role the region reads. A part's templates (``Part.files``) write
    besides them its own classes and those of the parts it draws
    (``Part.written``); a page writes nothing else."""

    written: dict[str, frozenset[str]] = {}
    for part in parts():
        for name in part.files():
            written[name] = written.get(name, frozenset()) | part.written()
    shared = public_classes() | set(ROLE_CLASSES)
    return {
        (name, word)
        for name, text in _templates().items()
        for _, word in class_words(text)
        if word not in shared and word not in written.get(name, ())
    }


def needless_classes() -> set[tuple[str, str]]:
    """Each class a part declares that neither its templates nor its shape
    writes, with the part's partial."""

    templates = _templates()
    needless = set()
    for part in parts():
        drawn = {word for name in part.files() for _, word in class_words(templates.get(name, ""))}
        shaped = inspect.getsource(part.shape) if part.shape else ""
        needless |= {(part.partial, word) for word in part.owned() - drawn if f'"{word}"' not in shaped}
    return needless


def _templates() -> dict[str, str]:
    return {
        path.relative_to(TEMPLATES).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(TEMPLATES.rglob("*.html"))
    }
