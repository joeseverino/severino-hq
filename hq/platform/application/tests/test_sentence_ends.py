"""A sentence a page shows closes through ``ended``, never on a bare period after a value.

One rule for the host and its extensions, kept in ``hq_sdk.validation``, which
says what it refuses and what it leaves bare. This holds the host's code that
builds page text to it: ``hq/platform/application`` and ``hq/domains``, less
their tests, migrations and management commands.
"""

import ast
import io
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, tag

from hq_sdk import ui as sdk_ui, validation
from hq_sdk.validation import _SENTENCE_LEFT_OUT, _closes_by_hand, sentence_closes

from ..ui import ended
from .source import parsed, source_files

ROOT = Path(__file__).resolve().parents[4]
COVERED = ("hq/platform/application", "hq/domains")


def _files() -> list[Path]:
    return [
        path
        for covered in COVERED
        for path in source_files(ROOT / covered, "*.py")
        if not _SENTENCE_LEFT_OUT & set(path.relative_to(ROOT).parts)
    ]


@tag("source", "tree")
class SentenceEndTests(SimpleTestCase):
    def test_a_page_sentence_closes_on_a_value_through_ended(self):
        """Write ``ended(f"... {value}")``: a value that ends in its own period keeps only that one."""

        files = _files()
        self.assertGreater(len(files), 100, "the sentence-end check read almost nothing")
        found = [
            f"{path.relative_to(ROOT)}:{line}: {value}"
            for path in files
            for line, value in _closes_by_hand(parsed(path))
        ]
        self.assertEqual(found, [])


class SentenceEndRuleTests(SimpleTestCase):
    """The rule over fixtures: what it refuses, and what it leaves bare."""

    def _found(self, source: str) -> list[str]:
        return [value for _line, value in _closes_by_hand(ast.parse(source))]

    def test_a_sentence_closing_on_a_value_is_refused(self):
        self.assertEqual(self._found('detail = f"Renews itself through {registrar}."'), ["registrar"])
        self.assertEqual(
            self._found("detail = f\"Renews at {registrar or 'its registrar'}.\""), ["registrar or 'its registrar'"]
        )

    def test_a_sentence_ending_inside_the_text_is_refused(self):
        self.assertEqual(self._found('body = f"Issued by {issuer}. It renews itself."'), ["issuer"])

    def test_a_dot_inside_a_name_is_no_sentence_end(self):
        self.assertEqual(self._found('a = f"{host}.{zone}"\nb = f"{name}.json"\nc = name.removeprefix(f"{noun}.")'), [])
        self.assertEqual(self._found('d = destination.name + ".part"\ne = stem + f".{suffix}"'), [])

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


class ExtensionSentenceTests(SimpleTestCase):
    """An extension's tree is read by the same rule, where ``ended`` reaches it."""

    def _tree(self, directory: str, files: dict[str, str]) -> Path:
        root = Path(directory)
        for name, source in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(source, encoding="utf-8")
        return root

    def test_an_extension_closes_a_sentence_with_the_hosts_own_ended(self):
        self.assertIs(sdk_ui.ended, ended)

    def test_a_sentence_closed_by_hand_is_found_where_page_text_is_built(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._tree(
                directory,
                {
                    "example/views.py": 'detail = f"Logged by {device}."\nkept = ended(f"Logged by {device}")\n',
                    "example/tests/test_views.py": 'expected = f"Logged by {device}."\n',
                    "example/management/commands/load.py": 'said = f"Loaded {path}."\n',
                },
            )

            self.assertEqual(sentence_closes(root), ["example/views.py:1: device"])

    def test_the_plugin_check_refuses_a_tree_that_closes_one_by_hand(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._tree(directory, {"example/views.py": 'detail = f"Logged by {device}."\n'})
            said = io.StringIO()
            with redirect_stdout(said), patch("sys.argv", ["hq_sdk.validation", str(root)]):
                status = validation.main()

        self.assertEqual(status, 1)
        self.assertIn("example/views.py:1: device", said.getvalue())
