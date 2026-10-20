"""Compose builds only from a composition set it can read, and masks every name an extension goes by."""

import json
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from tools.pipeline import composition_set

ALPHA = {"id": "example.alpha_one", "repository": "example/alpha-extensions", "path": "packages/alpha-one"}
GAMMA = {"id": "example.gamma", "repository": "example/gamma"}


class WriteTests(SimpleTestCase):
    def write(self, secret: str) -> tuple[int, str, Path]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        target = Path(directory.name) / "composition-set.json"
        printed = StringIO()
        with mock.patch.dict("os.environ", {"COMPOSITION_EXTENSIONS": secret}), redirect_stdout(printed):
            status = composition_set.main(["write", str(target)])
        return status, printed.getvalue(), target

    def test_a_readable_set_is_written_as_given(self):
        for composition in ({"extensions": [ALPHA, GAMMA], "retired": ["example.beta"]}, {"extensions": []}):
            with self.subTest(composition=composition):
                secret = json.dumps(composition)
                status, _, target = self.write(secret)
                self.assertEqual(status, 0)
                self.assertEqual(target.read_text(), secret)

    def test_a_set_compose_cannot_build_is_refused(self):
        unset = "must be set as a repository secret"
        unlisted = "must be JSON with an extensions list"
        retired = "must list retired extensions as ids it no longer composes"
        outside = "path as a relative path inside its repository"
        cases = [
            (unset, ""),
            (unlisted, "not json"),
            (unlisted, json.dumps({"extension": []})),
            (retired, json.dumps({"extensions": [GAMMA], "retired": ["example.gamma"]})),
            (retired, json.dumps({"extensions": [], "retired": [""]})),
            (retired, json.dumps({"extensions": [], "retired": "example.gamma"})),
            (outside, json.dumps({"extensions": [{**GAMMA, "path": "../other"}]})),
            (outside, json.dumps({"extensions": [{**GAMMA, "path": "/abs"}]})),
            (outside, json.dumps({"extensions": [{**GAMMA, "path": "a b"}]})),
            (outside, json.dumps({"extensions": [{**GAMMA, "path": ""}]})),
        ]
        for reason, secret in cases:
            with self.subTest(secret=secret):
                status, printed, target = self.write(secret)
                self.assertEqual(status, 1)
                self.assertIn("::error::", printed)
                self.assertIn(reason, printed)
                self.assertFalse(target.exists())


class MaskTests(SimpleTestCase):
    def masks(self, composition: dict) -> set[str]:
        return set(composition_set.masks(composition))

    def test_every_spelling_of_every_name_is_masked_in_every_case(self):
        masked = self.masks({"extensions": [ALPHA], "retired": ["example.beta_two"]})
        for name in (
            "example.alpha_one",
            "example/alpha-extensions",
            "alpha-extensions",
            "alpha_extensions",
            "alpha_one",
            "alpha-one",
            "example.beta_two",
            "beta-two",
        ):
            with self.subTest(name=name):
                self.assertLessEqual({name, name[0].upper() + name[1:], name.upper()}, masked)

    def test_an_extension_at_its_repositorys_root_masks_no_dot(self):
        self.assertNotIn(".", self.masks({"extensions": [GAMMA]}))
        self.assertEqual(self.masks({"extensions": []}), set())
