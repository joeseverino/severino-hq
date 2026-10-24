"""A sentence a page shows closes through ``ended``, never on a bare period after a value.

A value HQ did not write (a registrar's name, a provider's phrase, a hostname,
a label someone typed) can carry its own period: "Cloudflare, Inc." closed
with ``f"... {registrar}."`` reads "Cloudflare, Inc..". ``ui.ended`` adds the
full stop only when the text has none, so the rule is that no f-string in the
code that builds page text closes ``{value}.`` by hand.

What it covers: ``hq/platform/application`` and ``hq/domains``, the code that
builds insights, findings, health, request paths and the views' messages, less
their management commands. A value is left bare only where the source shows it
is HQ's own words: a ``!r`` or a format spec, an UPPER_CASE constant, one of
HQ's phrase functions (``OWN_PHRASES``), or a choice between two literals.

What it leaves out: the argument of a ``raise`` and a management command's
output. Those reach an operator reading a log or a terminal, carry HQ's own
names, and are not page text.
"""

import ast
from pathlib import Path

from django.test import SimpleTestCase, tag

from .source import parsed, source_files

ROOT = Path(__file__).resolve().parents[4]
COVERED = ("hq/platform/application", "hq/domains")
LEFT_OUT = frozenset({"tests", "migrations", "management"})

# HQ's own phrase functions: their words end on a literal HQ wrote.
OWN_PHRASES = frozenset(
    {"counted", "ago", "duration", "span", "when", "when_day", "when_exact", "expiry_phrase", "human_bytes"}
)
# Builders whose template is markup, where ``ended`` cannot reach the text.
MARKUP = frozenset({"format_html"})
# The helper itself, which is where the full stop is written.
CLOSERS = frozenset({"ended"})
# String methods whose argument is a name's prefix or suffix ("noun."), never a sentence.
AFFIXES = frozenset({"removeprefix", "removesuffix", "startswith", "endswith"})


def _name(node: ast.AST) -> str:
    return getattr(node, "id", None) or getattr(node, "attr", None) or ""


def _constant(node: ast.AST) -> bool:
    name = _name(node)
    return bool(name) and name.isupper()


def _hq_words(value: ast.AST) -> bool:
    """Whether the source shows the value is HQ's own words."""

    if isinstance(value, ast.FormattedValue):
        if value.conversion == ord("r") or value.format_spec is not None:
            return True
        value = value.value
    if isinstance(value, ast.Constant) or _constant(value):
        return True
    if isinstance(value, ast.Call):
        return _name(value.func) in OWN_PHRASES or (
            isinstance(value.func, ast.Attribute) and _constant(value.func.value)
        )
    if isinstance(value, ast.IfExp):
        return all(isinstance(branch, ast.Constant) for branch in (value.body, value.orelse))
    return False


def _ends_on(node: ast.AST) -> list[ast.AST]:
    """The values text ends on: none when it ends on a literal HQ wrote."""

    if isinstance(node, ast.Constant):
        return []
    if isinstance(node, ast.JoinedStr):
        return _ends_on(node.values[-1]) if node.values else []
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _ends_on(node.right)
    if isinstance(node, ast.IfExp):
        return _ends_on(node.body) + _ends_on(node.orelse)
    return [node]


def _openings(node: ast.AST) -> set[str]:
    """The characters text can open with; "" where the source cannot say."""

    if isinstance(node, ast.Constant):
        return {str(node.value)[:1]}
    if isinstance(node, ast.JoinedStr):
        return _openings(node.values[0]) if node.values else {""}
    if isinstance(node, ast.FormattedValue):
        return _openings(node.value)
    if isinstance(node, ast.IfExp):
        return _openings(node.body) | _openings(node.orelse)
    return {""}


def _opens_with_period(node: ast.AST) -> bool:
    return "." in _openings(node)


def _closes_sentence(text: str, after: ast.AST | None) -> bool:
    """Whether literal text right after a value ends a sentence there.

    "." at the end, or before a space or a value that opens with one; ".json"
    or the dot between two values of a hostname is no sentence end.
    """

    if not text.startswith("."):
        return False
    rest = text[1:]
    if rest:
        return rest[0].isspace() or (after is None and not rest.strip())
    return after is None or any(opening.isspace() for opening in _openings(after) if opening)


def _bare_closes(node: ast.AST) -> list[ast.AST]:
    """Each value a sentence closes on with a period written by hand."""

    if isinstance(node, ast.JoinedStr):
        parts = node.values
        return [
            value
            for at, value in enumerate(parts[:-1])
            if isinstance(value, ast.FormattedValue)
            and isinstance(parts[at + 1], ast.Constant)
            and _closes_sentence(parts[at + 1].value, parts[at + 2] if at + 2 < len(parts) else None)
        ]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) and _opens_with_period(node.right):
        return _ends_on(node.left)
    return []


def _template_close(node: ast.AST) -> bool:
    """A ``str.format`` template that closes a sentence on a field: "Saved: {target}."."""

    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.rstrip().endswith("}.")


def _left_out(tree: ast.AST) -> set[int]:
    """The nodes under a ``raise``, in a markup builder's or affix test's call, or in ``ended`` itself."""

    skipped: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Raise)
            or (isinstance(node, ast.Call) and _name(node.func) in MARKUP | AFFIXES)
            or (isinstance(node, ast.FunctionDef) and node.name in CLOSERS)
        ):
            skipped.update(id(inner) for inner in ast.walk(node))
    return skipped


def _shown(value: ast.AST) -> str:
    return ast.unparse(value.value if isinstance(value, ast.FormattedValue) else value)


def bare_closes(tree: ast.AST) -> list[tuple[int, str]]:
    """``(line, value)`` for each page sentence that closes on a value with a hand-written period."""

    skipped = _left_out(tree)
    found = []
    for node in ast.walk(tree):
        if id(node) in skipped:
            continue
        found.extend((node.lineno, _shown(value)) for value in _bare_closes(node) if not _hq_words(value))
        if _template_close(node):
            found.append((node.lineno, node.value))
    return found


def _files() -> list[Path]:
    return [
        path
        for covered in COVERED
        for path in source_files(ROOT / covered, "*.py")
        if not LEFT_OUT & set(path.relative_to(ROOT).parts)
    ]


@tag("source", "tree")
class SentenceEndTests(SimpleTestCase):
    def test_a_page_sentence_closes_on_a_value_through_ended(self):
        """Write ``ended(f"... {value}")``: a value that ends in its own period keeps only that one."""

        files = _files()
        self.assertGreater(len(files), 100, "the sentence-end check read almost nothing")
        found = [
            f"{path.relative_to(ROOT)}:{line}: {value}" for path in files for line, value in bare_closes(parsed(path))
        ]
        self.assertEqual(found, [])


class SentenceEndRuleTests(SimpleTestCase):
    """The rule over fixtures: what it refuses, and what it leaves bare."""

    def _found(self, source: str) -> list[str]:
        return [value for _line, value in bare_closes(ast.parse(source))]

    def test_a_sentence_closing_on_a_value_is_refused(self):
        self.assertEqual(self._found('detail = f"Renews itself through {registrar}."'), ["registrar"])
        self.assertEqual(
            self._found("detail = f\"Renews at {registrar or 'its registrar'}.\""), ["registrar or 'its registrar'"]
        )

    def test_a_sentence_ending_inside_the_text_is_refused(self):
        self.assertEqual(self._found('body = f"Issued by {issuer}. It renews itself."'), ["issuer"])

    def test_a_dot_inside_a_name_is_no_sentence_end(self):
        self.assertEqual(self._found('a = f"{host}.{zone}"\nb = f"{name}.json"\nc = name.removeprefix(f"{noun}.")'), [])

    def test_a_period_before_a_value_that_opens_a_sentence_is_refused(self):
        self.assertEqual(
            self._found("""body = f"Running as {where}.{f' Fixed in {fixed}' if fixed else ''}\""""), ["where"]
        )

    def test_a_period_added_after_the_value_is_refused(self):
        self.assertEqual(
            self._found('body = f"Runs {running}" + (f". {stage}." if stage else ".")'), ["running", "stage"]
        )
        self.assertEqual(self._found('body = "Through " + registrar + "."'), ["registrar"])

    def test_a_format_template_closing_on_a_field_is_refused(self):
        self.assertEqual(self._found('created_message = "Expense saved: {target}."'), ["Expense saved: {target}."])

    def test_ended_closes_it(self):
        self.assertEqual(self._found('detail = ended(f"Renews itself through {registrar}")'), [])

    def test_hq_own_words_stay_bare(self):
        source = "\n".join(
            (
                'a = f"From readings {ago(read_at)}."',
                'b = f"Plugin {name!r}."',
                'c = f"{share:.0%}."',
                'd = f"Then press {CHECK_AGAIN}."',
                'e = f"Then press {CONTROLLER_REFRESH.words()}."',
                "f = f\"It is {'due' if due else 'not due'}.\"",
            )
        )
        self.assertEqual(self._found(source), [])

    def test_an_exception_and_markup_are_left_out(self):
        source = "\n".join(
            (
                'raise ValueError(f"Unknown kind {kind}.")',
                'html = format_html("From {}.", link)',
            )
        )
        self.assertEqual(self._found(source), [])
