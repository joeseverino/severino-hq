"""A function that declares the stale-write token reads it.

HQ hands ``expected_updated_at`` only to a capability handler that names it,
so a declaration nothing reads is a parameter nobody needed.
"""

import ast
from pathlib import Path

from django.test import SimpleTestCase, tag

from ..integration_specs import STALE_WRITE_KEYWORD
from .source import parsed, source_files

ROOT = Path(__file__).resolve().parents[4]


def _declares(function: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> bool:
    arguments = function.args
    return any(
        argument.arg == STALE_WRITE_KEYWORD
        for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
    )


def _reads(function: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> bool:
    body = function.body if isinstance(function.body, list) else [function.body]
    return any(
        isinstance(node, ast.Name) and node.id == STALE_WRITE_KEYWORD and isinstance(node.ctx, ast.Load)
        for statement in body
        for node in ast.walk(statement)
    )


class StaleWriteTokenTests(SimpleTestCase):
    @tag("source", "tree")
    def test_every_function_that_declares_the_token_reads_it(self):
        unread = set()
        for package in ("hq", "hq_sdk"):
            for path in source_files(ROOT / package, "*.py"):
                if "tests" in path.relative_to(ROOT).parts:
                    continue
                for node in ast.walk(parsed(path)):
                    if (
                        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                        and _declares(node)
                        and not _reads(node)
                    ):
                        unread.add(f"{path.relative_to(ROOT).as_posix()}:{getattr(node, 'name', '<lambda>')}")

        self.assertEqual(unread, set())
