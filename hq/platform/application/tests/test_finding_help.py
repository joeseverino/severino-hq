"""Every item HQ raises comes with help: a step to press, run or do, the page
the work is done on, or the reason none of those can be offered. Never a
problem alone."""

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase

from ..cadence import CONTROLLER_REFRESH
from ..finding_model import Finding
from ..finding_steps import ByHand, Do, Does, Why
from ..findings import RULES, _no_help_reason
from ..plugins import checked_attention


class EveryItemComesWithHelpTests(SimpleTestCase):
    def test_a_source_item_with_no_help_is_refused(self):
        with self.assertRaisesRegex(ImproperlyConfigured, "no help"):
            checked_attention("example", (_finding(),))
        with self.assertRaisesRegex(ImproperlyConfigured, "no help"):
            checked_attention("example", (_finding(no_help_reason=" "),))

    def test_a_source_item_with_any_help_is_kept(self):
        helped = (
            _finding(steps=(Do("Renew it at the registrar."),)),
            _finding(fixes=(Does(CONTROLLER_REFRESH.name),)),
            _finding(no_help_reason="Only the registrar can."),
            _finding(held=True),
            # What only reports stays off the queue, so it needs none.
            Finding(rule="example", subject="example", title="Example", severity="neutral", explanation="Example."),
        )

        self.assertEqual(checked_attention("example", helped), helped)


def _finding(**fields) -> Finding:
    return Finding(
        rule=RULES[0].name, subject="example", title="Example", severity="attention", explanation="Example.", **fields
    )


class FindingHelpTests(SimpleTestCase):
    def test_a_finding_with_neither_remedy_nor_command_carries_its_rules_reason(self):
        self.assertEqual(_no_help_reason(_finding()), RULES[0].no_help_reason)

    def test_its_own_reason_wins_over_the_rules(self):
        self.assertEqual(_no_help_reason(_finding(no_help_reason="Specific.")), "Specific.")

    def test_a_remedy_or_a_command_needs_no_reason(self):
        remedied = _finding(fixes=(Does(CONTROLLER_REFRESH.name),))
        commanded = _finding(fixes=(ByHand("Run it", "example --fix", Why.NOT_GRANTED),))

        self.assertEqual((_no_help_reason(remedied), _no_help_reason(commanded)), ("", ""))

    def test_words_alone_still_say_why_hq_cannot_do_it(self):
        self.assertEqual(_no_help_reason(_finding(fixes=(Do("Go and figure it out"),))), RULES[0].no_help_reason)


class ExampleEstateTests(TestCase):
    def test_every_finding_the_example_estate_raises_says_what_was_read_and_the_bar_it_crossed(self):
        """Each finding's severity is one its rule declares a bar for, so its
        why never falls back to the evidence alone."""

        from hq.platform.core import example_estate
        from hq.platform.core.example_estate.testing import throwaway_media

        from ..attention import finding_why
        from ..findings import estate_findings, rule_for
        from ..security import cli_principal

        with throwaway_media():
            example_estate.seed()
        raised = estate_findings(principal=cli_principal())

        for finding in raised:
            with self.subTest(rule=finding.rule, severity=finding.severity, title=finding.title):
                rule = rule_for(finding.rule)
                self.assertIn(finding.severity, rule.bars)
                self.assertIn(rule.bars[finding.severity], finding_why(finding, rule))
                # Resolution leaves every finding a step to take.
                self.assertTrue(finding.steps)


class FindingWhyTests(SimpleTestCase):
    """A finding's why is worded once, from what was read and its rule's bar."""

    def test_every_bar_reads_as_what_was_read_and_the_bar_never_as_the_rule(self):
        from ..attention import finding_why

        for rule in RULES:
            for severity, bar in rule.bars.items():
                with self.subTest(rule=rule.name, severity=severity):
                    finding = Finding(
                        rule=rule.name,
                        subject="example",
                        title="Example",
                        severity=severity,
                        explanation="Example.",
                        evidence=(("Read", "a value HQ read"),),
                    )
                    why = finding_why(finding, rule)
                    self.assertIn("Read: a value HQ read", why)
                    self.assertIn(bar, why)
                    self.assertNotIn(rule.title, why)
                    self.assertNotIn(rule.name, bar)
                    # A clause after "urgent once", so the why closes it.
                    self.assertFalse(bar.endswith("."))

    def test_the_why_quotes_two_readings_and_counts_the_rest(self):
        from ..attention import finding_why

        rule = RULES[0]
        finding = Finding(
            rule=rule.name,
            subject="example",
            title="Example",
            severity=rule.severity,
            explanation="Example.",
            evidence=(("One", "1"), ("Two", "2"), ("Three", "3"), ("Four", "4")),
        )

        self.assertTrue(finding_why(finding, rule).startswith("One: 1; Two: 2 (and 2 more); "))
