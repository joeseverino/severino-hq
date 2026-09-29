"""Every item HQ raises comes with help: a remedy HQ runs, the exact command
to run, or HQ's specific reason it can offer neither. Never bare prose."""

from __future__ import annotations

import ast
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from ..finding_model import Finding, FindingRule, OperatorStep, Remedy
from ..findings import RULES, _no_help_reason
from ..item_help import (
    COMMAND,
    REASON,
    REMEDY,
    cannot_help,
    commands,
    finding_help,
    finding_plan,
    item_help,
)
from ..ui import Insight
from ..workflow_contracts import ActionLink

ROOT = Path(__file__).parent.resolve().parent.parent
# Owned by a change in flight elsewhere; each is listed here, by file, until
# its items carry help, and the test below fails when one no longer needs it.
PENDING: set[str] = set()


def _insight_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) == "Insight"
    ]


def _unhelped(call: ast.Call) -> bool:
    """Neither ``workflow=`` nor ``actions=`` given, nor any ``**`` that could."""

    names = {keyword.arg for keyword in call.keywords}
    return not ({"workflow", "actions", None} & names)


def host_modules() -> list[Path]:
    """Every non-test module of every host package. ``hq_sdk`` only re-exports."""

    packages = {path.parent for path in ROOT.glob("*/__init__.py")} - {ROOT / "hq_sdk"}
    return sorted(
        path
        for package in packages
        for path in package.rglob("*.py")
        if not path.name.startswith("test") and "tests" not in path.parts and "migrations" not in path.parts
    )


class EveryItemComesWithHelpTests(SimpleTestCase):
    def test_every_attention_item_the_host_builds_carries_help(self):
        missing = [
            f"{path.relative_to(ROOT)}:{call.lineno}"
            for path in host_modules()
            if str(path.relative_to(ROOT)) not in PENDING
            for call in _insight_calls(path)
            if _unhelped(call)
        ]

        self.assertEqual(missing, [], "Give each a remedy, a command or a reason (application.item_help).")

    def test_a_pending_file_is_still_pending(self):
        for name in PENDING:
            with self.subTest(name=name):
                self.assertTrue(
                    any(_unhelped(call) for call in _insight_calls(ROOT / name)),
                    f"{name} carries help now; take it out of PENDING.",
                )

    def test_every_finding_rule_says_why_hq_cannot_resolve_it(self):
        for rule in RULES:
            with self.subTest(rule=rule.name):
                self.assertTrue(rule.no_help_reason.strip())
                self.assertNotIn("—", rule.no_help_reason)

    def test_a_rule_without_a_reason_is_refused(self):
        with self.assertRaisesRegex(ValueError, "why HQ cannot"):
            FindingRule("example", "Example", "attention", lambda estate: (), "Do it.", no_help_reason=" ")


def _finding(**fields) -> Finding:
    return Finding(rule=RULES[0].name, subject="example", title="Example", severity="attention",
                   explanation="Example.", **fields)


class FindingHelpTests(SimpleTestCase):
    def test_a_finding_with_neither_remedy_nor_command_carries_its_rules_reason(self):
        self.assertEqual(_no_help_reason(_finding()), RULES[0].no_help_reason)
        self.assertEqual(finding_help(_finding(no_help_reason=_no_help_reason(_finding()))), REASON)

    def test_its_own_reason_wins_over_the_rules(self):
        self.assertEqual(_no_help_reason(_finding(no_help_reason="Specific.")), "Specific.")

    def test_a_remedy_or_a_command_needs_no_reason(self):
        remedied = _finding(remedies=(Remedy("example.fix", "example", "Fix", ""),))
        commanded = _finding(steps=(OperatorStep("Run it", command="example --fix"),))

        self.assertEqual((_no_help_reason(remedied), _no_help_reason(commanded)), ("", ""))
        self.assertEqual((finding_help(remedied), finding_help(commanded)), (REMEDY, COMMAND))

    def test_a_label_without_a_command_is_not_help(self):
        prose = _finding(steps=(OperatorStep("Go and figure it out"),))

        self.assertEqual(finding_help(prose), "")
        self.assertEqual(_no_help_reason(prose), RULES[0].no_help_reason)

    def test_the_queue_item_carries_the_findings_commands_or_its_reason(self):
        commanded = _finding(steps=(OperatorStep("Run it", command="example --fix"),))
        reasoned = _finding(no_help_reason="Specific.")

        self.assertEqual(item_help(_item(workflow=finding_plan(commanded, "k"))), COMMAND)
        self.assertEqual(item_help(_item(workflow=finding_plan(reasoned, "k"))), REASON)


def _item(**fields) -> Insight:
    return Insight(status="attention", eyebrow="Example", title="Example", value="1", body="", **fields)


class ItemHelpTests(SimpleTestCase):
    def test_each_kind_of_help_is_recognised(self):
        remedy = ActionLink("remedy", "Fix", "remote_write", "/fix/", capability="example.fix")
        posted = ActionLink("approve", "Approve", "remote_write", "/approve/", method="POST")

        self.assertEqual(item_help(_item(actions=(remedy,))), REMEDY)
        self.assertEqual(item_help(_item(actions=(posted,))), REMEDY)
        self.assertEqual(item_help(_item(workflow=commands("k", (("Here", "example --fix"),)))), COMMAND)
        self.assertEqual(item_help(_item(workflow=cannot_help("k", "Because."))), REASON)

    def test_a_link_to_look_at_or_an_empty_reason_is_not_help(self):
        look = ActionLink("subject", "Open", "read", "/thing/")

        self.assertEqual(item_help(_item(actions=(look,))), "")
        self.assertEqual(item_help(_item(workflow=cannot_help("k", " "))), "")
        self.assertEqual(item_help(_item()), "")


class ComposedQueueTests(TestCase):
    """The queue as the dashboard composes it, every host item helped."""

    def test_every_item_in_the_composed_queue_carries_help(self):
        from assets.models import Asset
        from content.models import ContentItem
        from expenses.models import Expense
        from receipts.models import Receipt

        from ..domains import all_domains, domain_attention_items
        from .test_github_estate import store

        Expense.objects.create(date=timezone.localdate(), vendor="Vendor", item="Hosting",
                               category="hosting", total_cost=Decimal("12.00"))
        Receipt.objects.create(file="receipts/example.pdf", original_filename="example.pdf")
        Asset.objects.create(item_name="Unpriced", slug="unpriced", total_cost=Decimal("0"), category="tools")
        ContentItem.objects.create(title="Draft", slug="draft", status=ContentItem.Status.DRAFT)
        soon = (timezone.now() + timedelta(days=5)).isoformat()
        store(
            private=True, variables=["HQ_APP_CLIENT_ID"], checks={"state": "failure", "failing": ["lint"]},
            alerts={"code_scanning": {"critical": 1}}, artifacts=[{"name": "alpha-admission", "expires_at": soon}],
            access={"collaborators": [{"login": "example", "role": "admin"}], "deploy_keys": [], "token": "write",
                    "token_approves_reviews": True, "pinning_required": False, "security_fixes": False},
        )

        # The host's own domains: an extension's items are its own suite's to hold.
        host = {domain.id for domain in all_domains() if domain.origin == "host"}
        entries = [entry for entry in domain_attention_items() if entry["source_id"] in host]

        self.assertGreaterEqual(len(entries), 10)
        for entry in entries:
            with self.subTest(item=entry["item"].key):
                self.assertTrue(item_help(entry["item"]))
