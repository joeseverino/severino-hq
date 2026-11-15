from django.test import SimpleTestCase

from ..standards import Check, CheckResult, Measurement, Reading, measure


def _check(label, unmet, reading=None):
    found = reading or Reading.unavailable("Not read")
    return Check(id=label, label=label, test=lambda _subject: found, why="", fix="", unmet=unmet)


class SummaryTests(SimpleTestCase):
    def test_a_mixed_measurement_names_what_fails_and_leaves_out_what_was_not_read(self):
        measurement = Measurement(
            subject=None,
            results=(
                CheckResult(_check("Alerts on", "vulnerability alerts are off"), Reading.unmet()),
                CheckResult(_check("Main protected", "main is unprotected"), Reading.met()),
                CheckResult(_check("Secrets scanned", "secret scanning is off"), Reading.unmet()),
                CheckResult(_check("Plan feature", "the plan lacks it"), Reading.unavailable("Not offered")),
            ),
        )

        self.assertEqual(
            measurement.summary, "1 of 3 checks pass; vulnerability alerts are off, secret scanning is off"
        )

    def test_a_measurement_that_passes_says_only_how_many(self):
        measurement = Measurement(
            subject=None, results=(CheckResult(_check("Main protected", "unprotected"), Reading.met()),)
        )

        self.assertEqual(measurement.summary, "1 of 1 check passes")


class MeasureTests(SimpleTestCase):
    def test_a_check_failed_on_purpose_is_by_design_and_one_not_read_is_never_by_design(self):
        from ..standards import INTENDED

        standard = (
            _check("Port kept", "A host port is open", Reading.unmet()),
            _check("Plan feature", "The plan lacks it"),
        )

        found = measure(None, standard, {"Port kept": "It answers DNS.", "Plan feature": "Not offered."})

        self.assertEqual([result.state for result in found.results], [INTENDED, "unavailable"])
        self.assertEqual(found.unmet, ())

    def test_an_unenforced_standard_shows_an_unmet_check_quietly(self):
        standard = (_check("Alerts on", "Alerts are off", Reading.unmet()),)

        (enforced,) = measure(None, standard).results
        (quiet,) = measure(None, standard, enforced=False).results

        self.assertEqual((enforced.tone, quiet.tone), ("attention", "neutral"))


class GapTests(SimpleTestCase):
    def test_gaps_are_the_queues_rows_serious_first_and_by_design_last(self):
        from dataclasses import replace

        measurement = Measurement(
            subject=None,
            results=(
                CheckResult(_check("Alerts on", "Alerts are off"), Reading.unmet()),
                CheckResult(_check("Port kept", "A host port is open"), Reading.unmet(), "It answers DNS."),
                CheckResult(replace(_check("Only you", "Others have access"), serious=True), Reading.unmet()),
                CheckResult(_check("Main protected", "Main is unprotected"), Reading.met()),
            ),
        )

        rows = measurement.gaps

        self.assertEqual(
            [(row["label"], row["status"]) for row in rows],
            [
                ("Others have access", "serious"),
                ("Alerts are off", "attention"),
                ("A host port is open by design", "good"),
            ],
        )
        self.assertEqual(rows[2]["detail"], "It answers DNS.")
