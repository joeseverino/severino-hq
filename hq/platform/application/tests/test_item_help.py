"""Every item HQ raises comes with help: a remedy HQ runs, the exact command
to run, the page the work is done on, what to do in words, or the reason none
of those can be offered. Never a problem alone. Each Insight the host source
builds is held to it by the ast-grep rule attention-item-carries-help."""

from datetime import timedelta
from decimal import Decimal

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from hq.platform.application.contracts import Effect

from ..card_items import Insight
from ..finding_model import Finding, FindingRemedy, FindingRule, OperatorStep
from ..findings import RULES, _no_help_reason
from ..item_help import CANNOT, DO, REMEDY, RUN, cannot_help, commands, finding_plan, instructions
from ..workflow_contracts import ActionLink, WorkflowPlan

# The kinds of help an item can carry, as ``item_help`` names them.
COMMAND = "command"
PLACE = "place"
INSTRUCTION = "instruction"
REASON = "reason"


def _plan_help(plan: WorkflowPlan | None) -> str:
    kinds = set()
    for step in plan.steps if plan is not None else ():
        if step.phase == "act" and any(action.recommended for action in step.actions):
            kinds.add(REMEDY)
        elif step.phase == RUN and step.summary.strip():
            kinds.add(COMMAND)
        elif step.phase == DO and step.summary.strip():
            kinds.add(INSTRUCTION)
        elif step.phase == CANNOT and step.summary.strip():
            kinds.add(REASON)
    return next((kind for kind in (REMEDY, COMMAND, INSTRUCTION, REASON) if kind in kinds), "")


def item_help(item: Insight) -> str:
    """Which help an action item carries, by the names above; blank for none.

    The gauge the contract reads: a remedy is an action that names a capability
    or posts to its owner's route, and anything else on ``actions`` is only a
    link to look at. A place is the item's own next step with the page it is
    done on.
    """

    if any(action.capability or action.method == "POST" for action in item.actions):
        return REMEDY
    planned = _plan_help(item.workflow)
    if planned in (REMEDY, COMMAND):
        return planned
    if item.action.strip() and item.url:
        return PLACE
    return planned


class EveryItemComesWithHelpTests(SimpleTestCase):
    def test_every_finding_rule_says_why_hq_cannot_resolve_it(self):
        for rule in RULES:
            with self.subTest(rule=rule.name):
                self.assertTrue(rule.no_help_reason.strip())
                self.assertNotIn("\u2014", rule.no_help_reason)

    def test_a_rule_without_a_reason_is_refused(self):
        with self.assertRaisesRegex(ValueError, "why HQ cannot"):
            FindingRule("example", "Example", "attention", lambda estate: (), "Do it.", no_help_reason=" ")

    def test_a_rule_without_the_bar_for_its_severity_is_refused(self):
        with self.assertRaisesRegex(ValueError, "bar its attention findings cross"):
            FindingRule(
                "example",
                "Example",
                "attention",
                lambda estate: (),
                "Do it.",
                no_help_reason="HQ cannot.",
                bars={"serious": "it is worse"},
            )


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
        remedied = _finding(remedies=(FindingRemedy("example.fix", "example", "Fix", ""),))
        commanded = _finding(steps=(OperatorStep("Run it", command="example --fix"),))

        self.assertEqual((_no_help_reason(remedied), _no_help_reason(commanded)), ("", ""))

    def test_a_label_without_a_command_is_not_help(self):
        prose = _finding(steps=(OperatorStep("Go and figure it out"),))

        self.assertEqual(_no_help_reason(prose), RULES[0].no_help_reason)

    def test_the_queue_item_carries_the_findings_commands_or_its_reason(self):
        commanded = _finding(steps=(OperatorStep("Run it", command="example --fix"),))
        reasoned = _finding(no_help_reason="Specific.")

        self.assertEqual(item_help(_item(workflow=finding_plan(commanded, "k"))), COMMAND)
        self.assertEqual(item_help(_item(workflow=finding_plan(reasoned, "k"))), REASON)

    def test_a_finding_with_no_command_says_what_to_do_in_words(self):
        told = _finding(steps=(OperatorStep("Renew it at the registrar."),), no_help_reason="Only they can.")
        plan = finding_plan(told, "k")

        self.assertEqual(
            [(step.phase, step.summary) for step in plan.steps],
            [("do", "Renew it at the registrar."), ("cannot", "Only they can.")],
        )
        self.assertEqual(item_help(_item(workflow=plan)), INSTRUCTION)


def _item(**fields) -> Insight:
    return Insight(status="attention", eyebrow="Example", title="Example", value="1", body="", **fields)


class ItemHelpTests(SimpleTestCase):
    def test_each_kind_of_help_is_recognized(self):
        remedy = ActionLink("remedy", "Fix", "remote_write", "/fix/", capability="example.fix")
        posted = ActionLink("approve", "Approve", "remote_write", "/approve/", method="POST")

        self.assertEqual(item_help(_item(actions=(remedy,))), REMEDY)
        self.assertEqual(item_help(_item(actions=(posted,))), REMEDY)
        self.assertEqual(item_help(_item(workflow=commands("k", (("Here", "example --fix"),)))), COMMAND)
        self.assertEqual(item_help(_item(workflow=cannot_help("k", "Because."))), REASON)
        self.assertEqual(item_help(_item(workflow=instructions("k", "Sign in again."))), INSTRUCTION)
        self.assertEqual(item_help(_item(action="Fill in details", url="/assets/")), PLACE)

    def test_a_link_to_look_at_or_an_empty_reason_is_not_help(self):
        look = ActionLink("subject", "Open", Effect.READ, "/thing/")

        self.assertEqual(item_help(_item(actions=(look,))), "")
        self.assertEqual(item_help(_item(workflow=cannot_help("k", " "))), "")
        self.assertEqual(item_help(_item(action="Fill in details")), "")
        self.assertEqual(item_help(_item()), "")


class ComposedQueueTests(TestCase):
    """The queue as the dashboard composes it, every host item helped and explained."""

    def test_every_item_in_the_composed_queue_carries_help_and_says_why(self):
        from hq.domains.assets.models import Asset
        from hq.domains.content.models import ContentItem
        from hq.domains.expenses.models import Expense
        from hq.domains.receipts.models import Receipt

        from ..dashboard import queue_item
        from ..decisions import decision
        from ..domains import all_domains, domain_attention_items
        from .test_github_estate import store

        Expense.objects.create(
            date=timezone.localdate(), vendor="Vendor", item="Hosting", category="hosting", total_cost=Decimal("12.00")
        )
        Receipt.objects.create(file="receipts/example.pdf", original_filename="example.pdf")
        Asset.objects.create(item_name="Unpriced", slug="unpriced", total_cost=Decimal(0), category="tools")
        ContentItem.objects.create(title="Draft", slug="draft", status=ContentItem.Status.DRAFT)
        soon = (timezone.now() + timedelta(days=5)).isoformat()
        store(
            private=True,
            variables=["HQ_APP_CLIENT_ID"],
            checks={"state": "failure", "failing": ["lint"]},
            alerts={"code_scanning": {"critical": 1}},
            artifacts=[{"name": "alpha-admission", "expires_at": soon}],
            access={
                "collaborators": [{"login": "example", "role": "admin"}],
                "deploy_keys": [],
                "token": "write",
                "token_approves_reviews": True,
                "pinning_required": False,
                "vulnerability_alerts": False,
            },
        )

        # The host's own domains: an extension's items are its own suite's to hold.
        host = {domain.id for domain in all_domains() if domain.origin == "host"}
        entries = [entry for entry in domain_attention_items() if entry["source_id"] in host]

        self.assertGreaterEqual(len(entries), 10)
        for entry in entries:
            with self.subTest(item=entry["item"].key):
                self.assertTrue(item_help(entry["item"]))
                # The panel behind its urgency leads with why, in words.
                shown = decision(queue_item(entry["source_id"], entry["source"], entry["item"]))
                self.assertTrue(shown["origin"]["why"].strip())

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

        self.assertGreaterEqual(len({finding.rule for finding in raised}), 5)
        for finding in raised:
            with self.subTest(rule=finding.rule, severity=finding.severity, title=finding.title):
                rule = rule_for(finding.rule)
                self.assertIn(finding.severity, rule.bars)
                self.assertIn(rule.bars[finding.severity], finding_why(finding, rule))


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
