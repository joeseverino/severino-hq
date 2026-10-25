"""Property tests of the parsers that read outside input.

Each reads text HQ did not write: an image reference a container reports, a
package URL an SBOM lists, a provenance source a build attests. Examples
cover the shapes seen so far; these generate thousands of inputs each, so a
shape nobody thought of fails here rather than in a sweep.

Hypothesis is a development tool (the tools dependency group), not part of the
image, so inside the image these are skipped and the suite runs them on every
interpreter in the CI matrix.
"""

import random
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

from ..attestations import github_source
from ..images import ImageRef
from ..osv import query_of

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

    # A function rather than a test method: Hypothesis's fuzz_one_input calls
    # the test with no arguments of its own, so a method's `self` is missing.
    # The fuzzing harness (tests/fuzz/parsers.py) drives this same property.
    @FAST
    @given(st.text())
    def every_parser_answers_any_text(text):
        """Every parser takes any text and answers, or declines; none raises."""

        image = ImageRef.parse(text)
        if image is not None:
            assert image.repository == image.repository.lower()
            assert image.name
        query = query_of(text)
        if query is not None:
            assert "package" in query
        source = github_source(text)
        assert source == "" or source.count("/") == 1


@skipUnless(HYPOTHESIS, "hypothesis is a development tool")
class AnyTextProperties(SimpleTestCase):
    def test_every_parser_answers_any_text(self):
        every_parser_answers_any_text()

    def test_the_fuzzer_entry_point_takes_any_bytes(self):
        fuzz_one_input = every_parser_answers_any_text.hypothesis.fuzz_one_input
        seeds = random.Random(0)
        for seed in (b"", bytes(range(256)), *(seeds.randbytes(64) for _ in range(20))):
            with self.subTest(seed=seed[:16]):
                fuzz_one_input(seed)


@skipUnless(HYPOTHESIS, "hypothesis is a development tool")
class ImageRefProperties(SimpleTestCase):
    if HYPOTHESIS:

        @FAST
        @given(SEGMENT, st.lists(SEGMENT, min_size=1, max_size=3), TAG)
        def test_a_registry_reference_comes_apart_as_written(self, host, path, tag):
            registry = f"{host}.example"
            repository = "/".join(path)
            parsed = ImageRef.parse(f"{registry}/{repository}:{tag}")

            self.assertEqual((parsed.registry, parsed.repository, parsed.tag), (registry, repository, tag))


@skipUnless(HYPOTHESIS, "hypothesis is a development tool")
class PackageUrlProperties(SimpleTestCase):
    if HYPOTHESIS:

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
        @given(
            OWNER,
            REPOSITORY,
            st.sampled_from(["https://github.com/{}/{}", "https://github.com/{}/{}.git", "git@github.com:{}/{}.git"]),
        )
        def test_every_way_of_writing_a_repository_names_it(self, owner, repository, form):
            self.assertEqual(github_source(form.format(owner, repository)), f"{owner}/{repository}")
