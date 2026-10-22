from django.test import SimpleTestCase

from ..standards import MET, UNAVAILABLE, UNMET, Check, CheckResult, Measurement


def _check(label, unmet):
    return Check(id=label, label=label, test=lambda _subject: None, why="", fix="", unmet=unmet)


class SummaryTests(SimpleTestCase):
    def test_a_mixed_measurement_names_what_fails_and_leaves_out_what_was_not_read(self):
        measurement = Measurement(
            subject=None,
            results=(
                CheckResult(_check("Alerts on", "vulnerability alerts are off"), UNMET),
                CheckResult(_check("Main protected", "main is unprotected"), MET),
                CheckResult(_check("Secrets scanned", "secret scanning is off"), UNMET),
                CheckResult(_check("Plan feature", "the plan lacks it"), UNAVAILABLE),
            ),
        )

        self.assertEqual(
            measurement.summary, "1 of 3 checks pass; vulnerability alerts are off, secret scanning is off"
        )

    def test_a_measurement_that_passes_says_only_how_many(self):
        measurement = Measurement(subject=None, results=(CheckResult(_check("Main protected", "unprotected"), MET),))

        self.assertEqual(measurement.summary, "1 of 1 check passes")
