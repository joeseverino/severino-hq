"""`manage.py api_openapi --check` fails on a stale or missing document, and writes nothing."""

import json
import re
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, tag

from hq.platform.api.management.commands import api_openapi as command
from hq.platform.api.openapi import document, render
from tests.mise_tasks import mise_tasks


class CheckTests(SimpleTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name, "hq-api.openapi.json")
        patcher = patch.object(command, "DOCUMENT_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def check(self, err: StringIO | None = None) -> None:
        call_command("api_openapi", "--check", stdout=StringIO(), stderr=err or StringIO())

    def test_the_current_document_passes(self):
        self.path.write_text(render(document()), encoding="utf-8")
        self.check()

    def test_a_stale_document_fails_names_the_difference_and_is_left_as_it_was(self):
        behind = document()
        removed = min(behind["paths"])
        del behind["paths"][removed]
        stale = render(behind)
        self.path.write_text(stale, encoding="utf-8")

        err = StringIO()
        with self.assertRaises(CommandError):
            self.check(err)

        self.assertIn(json.dumps(removed), err.getvalue())
        self.assertEqual(self.path.read_text(encoding="utf-8"), stale)

    def test_a_missing_document_fails(self):
        with self.assertRaises(CommandError):
            self.check()
        self.assertFalse(self.path.exists())

    @tag("source")
    def test_a_failure_names_the_task_that_brings_the_document_current(self):
        with self.assertRaises(CommandError) as raised:
            self.check()

        named = set(re.findall(r"`mise run ([\w:-]+)[^`]*`", str(raised.exception)))
        self.assertIn("manage", named)
        self.assertLessEqual(named, mise_tasks().keys())
