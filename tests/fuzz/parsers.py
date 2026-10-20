"""Coverage-guided fuzzing of the parsers that read outside input.

The property tests in ``hq/platform/application/tests/test_properties.py`` generate inputs from
a strategy; this lets a fuzzer grow them from coverage instead, which finds
the shapes a strategy did not think to describe. The same four parsers:
image references, package URLs, provenance sources and failure logs.

    pip install atheris
    DJANGO_SETTINGS_MODULE=hq.config.settings python -m tests.fuzz.parsers -max_total_time=300

``test_one_input`` is also run by the suite over a fixed set of seeds
(``hq/platform/application/tests/test_fuzz_harness.py``), so the harness cannot rot unnoticed.
"""

import sys

import atheris

with atheris.instrument_imports():
    import django

    django.setup()

    from hq.platform.application.attestations import github_source
    from hq.platform.application.images import ImageRef
    from hq.platform.application.osv import query_of
    from tools.pipeline.ci_report import diagnose


def test_one_input(data: bytes) -> None:
    """Every parser takes any text and answers, or declines; none raises."""

    text = atheris.FuzzedDataProvider(data).ConsumeUnicodeNoSurrogates(4096)
    ImageRef.parse(text)
    query_of(text)
    github_source(text)
    found = diagnose(text)
    assert set(found) == {"id", "title", "fix"}


if __name__ == "__main__":
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()
