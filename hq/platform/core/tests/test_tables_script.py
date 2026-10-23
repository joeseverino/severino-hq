"""tables.js reads layout only in the places named here: the fit, which asks
whether a table is wider than its region before the frame is painted, and the
places that hold a table still once a reader has acted on it."""

import re
from pathlib import Path

from django.test import SimpleTestCase, tag

SCRIPT = Path(__file__).resolve().parents[4] / "static" / "js" / "tables.js"

# Where tables.js may read layout, and what each read is for. A function that
# only these call (`droppedCells` for `openDetail`) is part of its caller.
MEASURES_IN = {
    "overflows": "whether a table is wider than its region, the one question the fit asks",
    "shown": "whether a region has a width to fit at all",
    "isCut": "a row whose value is cut short, which only a layout knows; markCut marks it and moves nothing",
    "holdColumns": "every column at the width the reader saw while a row is open",
    "openDetail": "the open row's line beneath: what its row left out, each part under its own column",
    "pinColumnWidths": "the columns of a table a sort, search or page turn swaps in",
}

_MEASURES = re.compile(
    r"\b(?:getBoundingClientRect|getClientRects|getComputedStyle"
    r"|offset(?:Width|Height|Left|Top)|scroll(?:Width|Height)|client(?:Width|Height))\b"
)
_ARROW = re.compile(r"\b(?:const|let)\s+(\w+)\s*=\s*(?:async\s+)?(?:\([^()]*\)|\w+)\s*=>\s*")
_FUNCTION = re.compile(r"\bfunction\s+(\w+)\s*\([^()]*\)\s*")
# What is not code: a comment, a string, a template's text (what it
# interpolates is code), and a regular expression, which starts where a `/`
# cannot divide.
_NOT_CODE = re.compile(
    r"//[^\n]*"
    r"|/\*.*?\*/"
    r"|\"(?:\\.|[^\"\\\n])*\""
    r"|'(?:\\.|[^'\\\n])*'"
    # A `$` is an interpolation's or a character, never either, so a long
    # template cannot be read more than one way.
    r"|`(?:\\.|\$\{[^{}`]*\}|\$(?!\{[^{}`]*\})|[^`\\$])*`"
    r"|(?:[(,=:\[!&|?{};]|\breturn)\s*(?P<regex>/(?:\\.|\[(?:\\.|[^\]\\\n])*\]|[^/\\\n\[])+/[a-z]*)",
    re.DOTALL,
)
_INTERPOLATION = re.compile(r"\$\{([^{}`]*)\}")


def code_only(source: str) -> str:
    """``source`` with everything but code blanked to spaces, newlines kept, so
    what is left is code at the offsets it was written at."""

    out = list(source)
    for found in _NOT_CODE.finditer(source):
        start, end = found.span("regex") if found.group("regex") else found.span()
        kept = [part.span(1) for part in _INTERPOLATION.finditer(source, start, end)] if source[start] == "`" else []
        for at in range(start, end):
            if source[at] != "\n" and not any(first <= at < last for first, last in kept):
                out[at] = " "
    return "".join(out)


def _close(code: str, start: int, opening: str, closing: str) -> int:
    depth = 0
    for at in range(start, len(code)):
        depth += (code[at] == opening) - (code[at] == closing)
        if not depth:
            return at + 1
    raise ValueError(f"unbalanced {opening} from offset {start}")


def functions(code: str) -> list[tuple[str, int, int]]:
    """Every named function in ``code``: its name and its span, from the name
    to the end of its body."""

    found = []
    for definition in (*_ARROW.finditer(code), *_FUNCTION.finditer(code)):
        body = definition.end()
        if code[body] == "{":
            end = _close(code, body, "{", "}")
        else:
            # An expression body runs to the `;` that ends its statement.
            end, depth = body, 0
            while code[end] != ";" or depth:
                depth += (code[end] in "([{") - (code[end] in ")]}")
                end += 1
        found.append((definition.group(1), definition.start(), end))
    return found


def unheld_measurements(source: str) -> list[str]:
    """Each place ``source`` reads layout outside the places that hold a table
    still, as ``line: function``."""

    code = code_only(source)
    spans = functions(code)

    def inside(at: int) -> str:
        enclosing = [(end - start, name) for name, start, end in spans if start <= at < end]
        return min(enclosing)[1] if enclosing else "(no function)"

    def within(at: int, names: set[str]) -> bool:
        return any(start <= at < end for name, start, end in spans if name in names)

    allowed = set(MEASURES_IN)
    # A helper whose every call is in an allowed place measures for that place.
    while True:
        lent = {
            name
            for name, start, end in spans
            if name not in allowed
            and (calls := [m.start() for m in re.finditer(rf"\b{name}\b", code) if not start <= m.start() < end])
            and all(within(at, allowed) for at in calls)
        }
        if not lent - allowed:
            break
        allowed |= lent
    return [
        f"{code.count(chr(10), 0, found.start()) + 1}: {inside(found.start())}"
        for found in _MEASURES.finditer(code)
        if not within(found.start(), allowed)
    ]


@tag("source")
class TablesScriptTests(SimpleTestCase):
    def test_the_table_script_measures_only_where_named(self):
        self.assertEqual(unheld_measurements(SCRIPT.read_text(encoding="utf-8")), [])

    def test_each_place_named_measures_and_one_added_elsewhere_is_found(self):
        source = SCRIPT.read_text(encoding="utf-8")
        code = code_only(source)
        measuring = {name for name, start, end in functions(code) if _MEASURES.search(code, start, end)}
        self.assertEqual(set(MEASURES_IN) - measuring, set())

        sorting = "const sortRows = (table, column, direction) => {"
        self.assertIn(sorting, source)
        added = source.replace(sorting, sorting + "\n    table.getBoundingClientRect();")
        self.assertEqual([found.split(": ")[1] for found in unheld_measurements(added)], ["sortRows"])
