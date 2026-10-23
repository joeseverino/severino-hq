"""Every script that turns a pipeline gate's output into its verdict is tested with known-bad input."""

from pathlib import Path

from django.test import SimpleTestCase, tag

PIPELINE = Path(__file__).resolve().parents[1]


def untested(pipeline: Path) -> list[str]:
    """The report scripts in ``pipeline`` with no ``tests/test_<name>.py`` beside them."""
    return sorted(
        script.name
        for script in pipeline.glob("*report*.py")
        if not (pipeline / "tests" / f"test_{script.stem}.py").is_file()
    )


@tag("source")
class VerdictCoverageTests(SimpleTestCase):
    def test_every_report_script_has_a_test_module(self):
        self.assertTrue(list(PIPELINE.glob("*report*.py")), f"no report script under {PIPELINE}")
        self.assertEqual(untested(PIPELINE), [])
