"""The pipeline's plain-English diagnoses (deploy/diagnoses.json)."""

import json
import re
import tempfile
from pathlib import Path
from unittest import skipUnless

from django.test import SimpleTestCase, tag

from tools.pipeline import ci_report

try:
    from hypothesis import given, settings, strategies as st
    from hypothesis.configuration import set_hypothesis_home_dir
except ImportError:  # pragma: no cover - a development tool
    HYPOTHESIS = False
else:
    HYPOTHESIS = True
    # Outside the checkout: a cache in the tree is text the host must never carry.
    set_hypothesis_home_dir(str(Path(tempfile.gettempdir()) / "severino-hq-hypothesis"))

IMAGE = "ghcr.io/example-owner/example-repo/composition@sha256:" + "a" * 64
STAMP = "2026-10-02T00:12:32.36Z "
SEALED = "scripts/failure-logs.sh"

# A log line, the diagnosis it is named as, and what its fix points at (None: any).
CASES = (
    (
        "#15 pushing\\ndenied: permission_denied: write_package\\n##[error]Process completed",
        "package-write",
        "Write role",
    ),
    ("health: starting\\nNew image did not become healthy.\\nRestoring previous image", "unhealthy", "previous image"),
    (
        "Controller connection preflight failed; inspect the private host log.\n"
        "Controller activation failed; rolling back application image.",
        "controller-preflight",
        "controller-preflight.log",
    ),
    # As hq-host deploy prints them (controller/host).
    (f"refusing to deploy {IMAGE}: not signed by this repository's compose workflow", "unsigned-composition", None),
    (
        f"refusing to deploy {IMAGE}: it drops 1 extension(s) the running release carries "
        "that its composition does not declare retired",
        "extension-dropped",
        None,
    ),
    (
        f"refusing to deploy {IMAGE}: could not read the extensions the running release carries",
        "extensions-unreadable",
        None,
    ),
    ("a deploy needs 512 MiB free; 100 MiB are", "disk", None),
    ("the new image did not become healthy: exit status 1", "unhealthy", None),
    ("something nobody has seen", "unknown", "deploy/diagnoses.json"),
    # As GitHub prints them: a title is not in the log, only the message.
    (f"{STAMP}##[error]2 extension test(s) failed; the full output is sealed", "composition-tests", SEALED),
    (
        f"{STAMP}##[error]The composed suite did not run: ImproperlyConfigured; the full",
        "composition-did-not-run",
        SEALED,
    ),
    (f"{STAMP}##[error]The admitted set does not compose. The reason is sealed.", "composition-refused", SEALED),
    (f"{STAMP}##[error]A coordinated branch did not build; its output is sealed.", "candidate-build", SEALED),
    (f"{STAMP}##[error]The container did not report healthy within 90 seconds.", "image-unhealthy", SEALED),
    # A message in an echoed script is not a failure.
    (
        "2026-10-02T00:01:00.1Z ##[group]Run set -euo pipefail\n"
        '2026-10-02T00:01:00.2Z echo "::error::COMPOSITION_EXTENSIONS must be set"\n'
        "2026-10-02T00:01:00.3Z env:\n"
        "2026-10-02T00:01:00.4Z ##[endgroup]\n"
        "2026-10-02T00:02:00.0Z FAILED (failures=1)\n",
        "host-tests",
        None,
    ),
    # A failing host test is not blamed on the composition.
    (f"{STAMP}FAILED (failures=1, skipped=2)", "host-tests", None),
    (f"{STAMP}composed suite: FAILED (failures=1)", "composition-tests", None),
)


@tag("source")
class DiagnoseTests(SimpleTestCase):
    def test_a_log_is_named_with_its_fix(self):
        diagnose = ci_report.diagnose
        for log, expected, points_at in CASES:
            with self.subTest(expected=expected, log=log[-40:]):
                found = diagnose(log)
                self.assertEqual(found["id"], expected)
                if points_at:
                    self.assertIn(points_at, found["fix"])

    def test_it_never_repeats_the_log(self):
        secret = "private-extension-name"
        found = ci_report.diagnose(f"{secret}: denied: permission_denied: write_package")

        self.assertNotIn(secret, json.dumps(found))

    def test_every_entry_is_complete_and_its_pattern_compiles(self):
        entries = json.loads(ci_report.CATALOG.read_text())["diagnoses"]
        ids = [entry["id"] for entry in entries]

        self.assertEqual(len(ids), len(set(ids)))
        for entry in entries:
            self.assertEqual(set(entry), {"id", "match", "title", "fix"})
            re.compile(entry["match"])


@tag("source")
@skipUnless(HYPOTHESIS, "hypothesis is a development tool")
class DiagnosisProperties(SimpleTestCase):
    if HYPOTHESIS:

        @settings(max_examples=300, deadline=None)
        @given(st.text())
        def test_a_diagnosis_only_ever_repeats_the_catalog(self, log):
            """Whatever the log says, nothing of it is printed: the catalog's
            text is safe on a public repository and the log's may not be."""

            allowed = [ci_report.UNKNOWN, *ci_report.diagnoses()]
            found = ci_report.diagnose(log)

            self.assertIn(found, [{key: entry[key] for key in ("id", "title", "fix")} for entry in allowed])
