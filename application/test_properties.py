"""Property tests of the parsers that read outside input.

Each reads text HQ did not write: an image reference a container reports, a
package URL an SBOM lists, a provenance source a build attests, a failed run's
log. Examples cover the shapes seen so far; these generate thousands of inputs
each, so a shape nobody thought of fails here rather than in a sweep.

Hypothesis is a development tool (requirements-tools.txt), not part of the
image, so inside the image these are skipped and the suite runs them on every
interpreter in the CI matrix.
"""

from __future__ import annotations

import importlib.util
import string
import tempfile
from pathlib import Path
from unittest import skipUnless

from django.test import SimpleTestCase

try:
    from hypothesis import given, settings, strategies as st
    from hypothesis.configuration import set_hypothesis_home_dir
except ImportError:  # pragma: no cover - the image does not carry it
    HYPOTHESIS = False
else:
    HYPOTHESIS = True
    # Outside the checkout. Hypothesis caches constants it finds in every
    # module it can import, installed extensions included, and a cache in the
    # tree is text the host must never carry.
    set_hypothesis_home_dir(str(Path(tempfile.gettempdir()) / "severino-hq-hypothesis"))

from .attestations import github_source
from .images import ImageRef
from .osv import query_of

ROOT = Path(__file__).resolve().parents[1]

if HYPOTHESIS:
    SEGMENT = st.text(string.ascii_lowercase + string.digits, min_size=1, max_size=12)
    OWNER = st.text(string.ascii_letters + string.digits + "-", min_size=1, max_size=20).filter(
        lambda text: not text.startswith("-")
    )
    REPOSITORY = st.text(string.ascii_letters + string.digits + "-_.", min_size=1, max_size=30).filter(
        lambda text: text not in {".", ".."} and not text.endswith(".git")
    )
    TAG = st.text(string.ascii_letters + string.digits + "._-", min_size=1, max_size=20)
    FAST = settings(max_examples=300, deadline=None)


def _diagnose():
    spec = importlib.util.spec_from_file_location("diagnose", ROOT / "scripts" / "diagnose.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@skipUnless(HYPOTHESIS, "hypothesis is a development tool")
class ImageRefProperties(SimpleTestCase):
    if HYPOTHESIS:

        @FAST
        @given(st.text())
        def test_any_text_parses_or_is_refused_never_raises(self, text):
            parsed = ImageRef.parse(text)
            if parsed is not None:
                self.assertEqual(parsed.repository, parsed.repository.lower())
                self.assertTrue(parsed.name)

        @FAST
        @given(SEGMENT, st.lists(SEGMENT, min_size=1, max_size=3), TAG)
        def test_a_registry_reference_comes_apart_as_written(self, host, path, tag):
            registry = f"{host}.example"
            repository = "/".join(path)
            parsed = ImageRef.parse(f"{registry}/{repository}:{tag}")

            self.assertEqual(
                (parsed.registry, parsed.repository, parsed.tag), (registry, repository, tag)
            )


@skipUnless(HYPOTHESIS, "hypothesis is a development tool")
class PackageUrlProperties(SimpleTestCase):
    if HYPOTHESIS:

        @FAST
        @given(st.text())
        def test_any_text_is_a_query_or_nothing_never_raises(self, text):
            query = query_of(text)
            if query is not None:
                self.assertIn("package", query)

        @FAST
        @given(SEGMENT, TAG, SEGMENT)
        def test_a_language_package_is_queried_without_its_qualifiers(self, name, version, arch):
            query = query_of(f"pkg:pypi/{name}@{version}?arch={arch}")

            self.assertTrue(query["package"]["purl"].startswith(f"pkg:pypi/{name}@"))
            self.assertNotIn("?", query["package"]["purl"])


@skipUnless(HYPOTHESIS, "hypothesis is a development tool")
class ProvenanceSourceProperties(SimpleTestCase):
    if HYPOTHESIS:

        @FAST
        @given(st.text())
        def test_any_source_is_one_repository_or_none(self, text):
            found = github_source(text)
            self.assertTrue(found == "" or found.count("/") == 1)

        @FAST
        @given(OWNER, REPOSITORY, st.sampled_from(["https://github.com/{}/{}", "https://github.com/{}/{}.git", "git@github.com:{}/{}.git"]))
        def test_every_way_of_writing_a_repository_names_it(self, owner, repository, form):
            self.assertEqual(github_source(form.format(owner, repository)), f"{owner}/{repository}")


@skipUnless(HYPOTHESIS, "hypothesis is a development tool")
class DiagnosisProperties(SimpleTestCase):
    if HYPOTHESIS:

        @FAST
        @given(st.text())
        def test_a_diagnosis_only_ever_repeats_the_catalog(self, log):
            """Whatever the log says, nothing of it is printed: the catalog's
            text is safe on a public repository and the log's may not be."""

            module = _diagnose()
            allowed = [module.UNKNOWN, *module.diagnoses()]
            found = module.diagnose(log)

            self.assertIn(
                found, [{key: entry[key] for key in ("id", "title", "fix")} for entry in allowed]
            )
