"""The request inspector words each fact once: no phrase is written in two of its modules.

The layer view, the hop view, the headers and the connection each describe the
same request, so a phrase two of them write is one fact worded twice, free to
drift. It lives as a constant or function in the module that owns the fact,
and the other module reads it there. A phrase is any string literal of two
words or more, each fixed part of an f-string included; a docstring is no
page text.
"""

import tempfile
from collections import defaultdict
from pathlib import Path

from django.test import SimpleTestCase, tag

from .facts import STRING, scan, strings

APPLICATION = Path(__file__).resolve().parents[1]
MODULES = "request_*.py"
# A string standing as a statement of its own is a docstring.
RULES = {"phrase": {"all": [STRING, {"not": {"inside": {"kind": "expression_statement"}}}]}}


def restated(directory: Path) -> dict[str, list[str]]:
    """Each phrase written in more than one request module, with the modules that write it."""

    written: dict[str, set[str]] = defaultdict(set)
    for found in scan(RULES, *sorted(directory.glob(MODULES))):
        for text in strings(found):
            if len(text.split()) > 1:
                written[text].add(found.path.name)
    return {phrase: sorted(paths) for phrase, paths in written.items() if len(paths) > 1}


@tag("source", "tree")
class RequestWordsTests(SimpleTestCase):
    def test_no_phrase_is_written_in_two_request_modules(self):
        self.assertTrue(list(APPLICATION.glob(MODULES)), f"no {MODULES} in {APPLICATION}")

        self.assertEqual(restated(APPLICATION), {})

    def test_a_phrase_written_twice_is_found(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "request_one.py").write_text(
                '"""A module docstring of many words."""\nDETAIL = f"{name} is not a trusted proxy."\n'
            )
            (root / "request_two.py").write_text(
                '"""A module docstring of many words."""\n'
                'CHECK = "Kept."\n'
                'def f(peer):\n    """A function docstring."""\n    return f"{peer} is not a trusted proxy."\n'
            )
            (root / "request_three.py").write_text('CHECK = "Kept."\n')

            self.assertEqual(restated(root), {" is not a trusted proxy.": ["request_one.py", "request_two.py"]})
