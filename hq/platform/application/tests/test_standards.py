from django.test import SimpleTestCase, TestCase

from ..standards import Check, CheckResult, Measurement, Reading, Standing, measure
from ..tone import Tone


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

        rows = measurement.finding_rows

        self.assertEqual(
            [(row["label"], row["status"]) for row in rows],
            [
                ("Others have access", "serious"),
                ("Alerts are off", "attention"),
                ("A host port is open by design", "good"),
            ],
        )
        self.assertEqual(rows[2]["detail"], "It answers DNS.")


class RaisesTests(SimpleTestCase):
    """Each of a standard's checks a subject was read to fail, by check or by
    subject; one met, not read or failed on purpose raises nothing."""

    def test_each_way_a_standard_raises(self):
        from dataclasses import replace

        from ..standard_findings import Measured, Per, Standard
        from ..tone import Tone

        drift, reach, unread, kept = (
            replace(_check(name, f"{name.title()} is off"), test=lambda said, name=name: said.get(name, Reading.met()))
            for name in ("drift", "reach", "unread", "kept")
        )
        reach = replace(reach, serious=True)
        checks = (drift, reach, unread, kept)
        not_read = Reading.unavailable("Not read")
        readings = {
            "alpha": {"drift": Reading.unmet(), "reach": Reading.unmet(), "unread": not_read},
            "beta": {"drift": Reading.unmet(), "kept": Reading.unmet(), "unread": not_read},
        }
        # Beta fails kept on purpose.
        measured = [
            Measured(measurement=measure(said, checks, {"kept": "On purpose."}), name=name, node=name, part=name)
            for name, said in readings.items()
        ]

        def raised(raises, held=checks):
            rule = Standard(
                name="example-standard",
                title="Example standard",
                severity=Tone.ATTENTION,
                checks=held,
                subjects=lambda _estate: measured,
                raises=raises,
                operator_action="Meet the check.",
                no_help_reason="HQ does not change it.",
                bars={Tone.ATTENTION: "a check is not met"},
            ).rule()
            self.assertTrue(rule.gathered, "its rule reads beyond the projection")
            return {
                (found.subject, found.part): (found.title, found.severity, dict(found.evidence).get("Not met in"))
                for found in rule.detect(None)
            }

        alpha = ("alpha", "alpha")
        cases = (
            (
                "by check",
                raised(Per.CHECK),
                {
                    ("", "drift"): ("Drift is off", Tone.ATTENTION, "alpha, beta"),
                    ("", "reach"): ("Reach is off", Tone.SERIOUS, "alpha"),
                },
            ),
            (
                "by subject",
                raised(Per.SUBJECT),
                {
                    alpha: ("alpha: drift is off, reach is off", Tone.SERIOUS, None),
                    ("beta", "beta"): ("beta: drift is off", Tone.ATTENTION, None),
                },
            ),
            ("only its checks", raised(Per.SUBJECT, (reach,)), {alpha: ("alpha: reach is off", Tone.SERIOUS, None)}),
        )
        for policy, found, expected in cases:
            with self.subTest(policy):
                self.assertEqual(found, expected)


class ExamplesTests(TestCase):
    """Every standard decided from its subject alone, walked through its
    examples: the kept record meets every check, one nothing was read for
    fails none, each check's breaks fail it (and the checks a ``Break`` names)
    at its seriousness with its fix written out where the standard writes one,
    and each of its keeps meets it."""

    def test_every_example_of_every_standard(self):
        from django.test import override_settings

        from .. import connection_security, container_standard, github_posture, request_standard, supply_chain
        from ..standards import Break

        for examples in (
            container_standard.EXAMPLES,
            github_posture.EXAMPLES,
            supply_chain.EXAMPLES,
            connection_security.EXAMPLES,
            request_standard.EXAMPLES,
        ):

            def measured(record, examples=examples):
                with override_settings(**record.get("settings", {})):
                    return measure(examples.subject(record), examples.checks)

            with self.subTest(kept=examples.checks[0].id):
                found = measured(examples.kept)
                self.assertEqual({result.state for result in found.results}, {Standing.MET})
            for record in examples.unread:
                with self.subTest(unread=record):
                    found = measured(record)
                    unread = [result for result in found.results if result.state == Standing.UNAVAILABLE]
                    self.assertEqual(found.unmet, ())
                    self.assertTrue(unread)
                    self.assertTrue(all((result.tone, result.icon) == (Tone.NEUTRAL, "clock") for result in unread))
            for check in examples.checks:
                for broken in check.breaks or ():
                    change, also = (broken.change, broken.also) if isinstance(broken, Break) else (broken, ())
                    with self.subTest(check=check.id, breaks=change):
                        record = examples.record(change)
                        unmet = measured(record).unmet
                        self.assertEqual(sorted(result.check.id for result in unmet), sorted((check.id, *also)))
                        (own,) = (result for result in unmet if result.check is check)
                        self.assertEqual(own.tone, Tone.SERIOUS if check.serious else Tone.ATTENTION)
                        if examples.fixed:
                            subject = examples.subject(record)
                            self.assertTrue(examples.fixed(subject, check), "its fix is written out, or why not")
                for change in check.keeps:
                    with self.subTest(check=check.id, keeps=change):
                        found = measured(examples.record(change))
                        self.assertEqual((found.state_of(check.id), found.unmet), (Standing.MET, ()))


class UnmetCheckRowTests(SimpleTestCase):
    """A check a subject does not meet reaches its page as a finding, so its
    row is worded and helped as every finding's is."""

    def test_its_fix_is_the_rows_step_and_it_links_to_no_findings_page(self):
        from dataclasses import replace

        check = replace(_check("Alerts on", "Alerts are off"), fix="Turn the alerts on.")
        measurement = Measurement(subject=None, results=(CheckResult(check, Reading.unmet()),))

        (row,) = measurement.finding_rows

        self.assertIn("Turn the alerts on.", str(row["steps"]))
        self.assertEqual(row["action"], "")
        # No rule the findings page lists raised it.
        self.assertEqual(row["url"], "")
