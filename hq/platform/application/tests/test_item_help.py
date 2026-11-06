"""Every item HQ raises comes with help: a remedy HQ runs, the exact command
to run, the page the work is done on, what to do in words, or the reason none
of those can be offered. Never a problem alone. Each Insight the host source
builds is held to it by the ast-grep rule attention-item-carries-help."""

from datetime import timedelta
from decimal import Decimal

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from ..finding_model import Finding, FindingRemedy, FindingRule, OperatorStep
from ..findings import RULES, _no_help_reason
from ..item_help import CANNOT, DO, REMEDY, RUN, cannot_help, commands, finding_plan, instructions
from ..ui import Insight
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
        look = ActionLink("subject", "Open", "read", "/thing/")

        self.assertEqual(item_help(_item(actions=(look,))), "")
        self.assertEqual(item_help(_item(workflow=cannot_help("k", " "))), "")
        self.assertEqual(item_help(_item(action="Fill in details")), "")
        self.assertEqual(item_help(_item()), "")


class ComposedQueueTests(TestCase):
    """The queue as the dashboard composes it, every host item helped."""

    def test_every_item_in_the_composed_queue_carries_help(self):
        from hq.domains.assets.models import Asset
        from hq.domains.content.models import ContentItem
        from hq.domains.expenses.models import Expense
        from hq.domains.receipts.models import Receipt

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
