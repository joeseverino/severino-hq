"""The fuzzing harness (fuzz/parsers.py) runs over fixed seeds on every suite.

Atheris is not installed here, so a stand-in provides the one call the harness
makes of it with input: decoding the fuzzer's bytes as text.
"""

import importlib.util
import sys
import types
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[4]
SEEDS = (
    b"",
    b"ghcr.io/example/app:v1.2.0@sha256:" + b"a" * 64,
    b"pkg:deb/debian/openssl@3.0.11?distro=debian-12&upstream=openssl",
    b"git@github.com:example/app.git",
    b"denied: permission_denied: write_package",
    bytes(range(256)),
)


class _Provider:
    def __init__(self, data: bytes):
        self.data = data

    def ConsumeUnicodeNoSurrogates(self, count: int) -> str:
        return self.data.decode("utf-8", "replace")[:count]


def _harness():
    stand_in = types.SimpleNamespace(
        instrument_imports=nullcontext, FuzzedDataProvider=_Provider, Setup=None, Fuzz=None
    )
    spec = importlib.util.spec_from_file_location("fuzz_parsers", ROOT / "tests/fuzz" / "parsers.py")
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"atheris": stand_in}):
        spec.loader.exec_module(module)
    return module


class FuzzHarnessTests(SimpleTestCase):
    def test_every_seed_is_answered_without_raising(self):
        harness = _harness()

        for seed in SEEDS:
            with self.subTest(seed=seed[:40]):
                harness.test_one_input(seed)
