"""A run's evidence is one part, each piece a labeled block its row's open line shows."""

from datetime import UTC, datetime

from django.template import Context, Template
from django.test import SimpleTestCase

from hq.platform.application.tests.markup import all_elements


def _drawn(evidence) -> dict[str, str]:
    """The words of each labeled block the part draws, by its label."""

    html = Template("{% run_evidence evidence %}").render(Context({"evidence": evidence}))
    return {
        element.attrs["data-label"]: element.words
        for element in all_elements(html)
        if "row-below" in element.classes and "data-label" in element.attrs
    }


class RunEvidenceTagTests(SimpleTestCase):
    def test_each_piece_of_a_run_s_evidence_is_a_labeled_block(self):
        from hq.platform.application.work_runs import RunEvidence

        drawn = _drawn(
            RunEvidence(
                hosts=("example-01",),
                correlation="example-investigation-7",
                worker="example-worker",
                lease_until=datetime(2026, 3, 2, 9, 35, tzinfo=UTC),
                events=(
                    {"host": "example-01", "task": "Example task", "outcome": "failed", "message": "Example <b>m</b>"},
                ),
            )
        )

        self.assertEqual(set(drawn), {"Machines", "Correlation", "Lease", "Tasks"})
        self.assertIn("example-investigation-7", drawn["Correlation"])
        self.assertIn("example-worker", drawn["Lease"])
        self.assertIn("Example task", drawn["Tasks"])
        self.assertIn("Example <b>m</b>", drawn["Tasks"])

    def test_a_run_with_no_evidence_draws_nothing(self):
        from hq.platform.application.work_runs import RunEvidence

        for evidence in (RunEvidence(), None):
            with self.subTest(evidence=evidence):
                self.assertEqual(_drawn(evidence), {})
