"""A record's audit rows are one story: each names the record it is about, the
controller's work on a queued request runs under the request's own id, and
each operation names the one that started it."""

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from hq.domains.control_plane.models import ApprovalRequest, ManagedResource, OperationRequest
from hq.domains.control_plane.tests import bridge_client
from hq.platform.application.adoption_testing import managing_everything
from hq.platform.application.capabilities import execute_capability, run_capability
from hq.platform.application.security import cli_principal, mcp_principal, web_principal
from hq.platform.core.attribution import bound, operation_context
from hq.platform.core.audit import audited_labels
from hq.platform.core.models import AuditLog

KEY = "example-rewrite"
REWRITE = {"domain": "app.example.com", "answer": "192.0.2.10"}
CONTROLLER = "example-controller"
SAID = "Rewrite applied."
# Host models whose rows are about the row's own record, which its object
# type and id already name, so they declare no other subject.
ABOUT_THEMSELVES = frozenset(
    {
        "assets.Asset",
        "calendars.Entry",
        "content.ContentItem",
        "control_plane.NotManaged",
        "control_plane.ReadRequest",
        "docs_index.DocumentationRecord",
        "expenses.Expense",
        "projects.Project",
        "receipts.Receipt",
    }
)


ADGUARD = [f"adguard.rewrite:{action}" for action in ("reconcile", "delete")]


def claim_and_report(*, success: bool = True) -> str:
    """The controller claims the queued request and reports it done, through the bridge."""

    operation = bridge_client.call("claim", controller_id=CONTROLLER, capability=ADGUARD)["operation"]
    bridge_client.call(
        "report",
        {
            "success": success,
            "observed_generation": asked_generation(operation["id"]),
            "status": {},
            "conditions": [],
            "message": SAID,
        },
        controller_id=CONTROLLER,
        operation=operation["id"],
    )
    return operation["id"]


def record_timeline(key: str):
    # Imported where it runs, so this module imports at a base without it.
    from hq.platform.application.record_timeline import record_timeline as story

    return story(key)


def asked_generation(operation_id: str) -> int:
    return OperationRequest.objects.get(pk=operation_id).input["generation"]


def queue_rows(operation_ids: list[str]) -> dict[str, AuditLog]:
    """Each request's queue row: the row that created it, by its id."""

    rows = AuditLog.objects.filter(
        object_type=audited_labels()[OperationRequest], object_id__in=operation_ids, action=AuditLog.Action.CREATED
    )
    return {row.object_id: row for row in rows}


class DeclaredSubjectTests(SimpleTestCase):
    def test_every_host_model_in_the_audit_names_its_subject_or_is_about_itself(self):
        from hq.platform.core.audit import audited_subjects

        audited = [model for model in audited_labels() if model.__module__.startswith("hq.")]

        self.assertTrue(audited)
        for model in audited:
            with self.subTest(model=model._meta.label):
                self.assertNotEqual(
                    model in audited_subjects(),
                    model._meta.label in ABOUT_THEMSELVES,
                    "a model's rows either name the record they are about or are about the row itself",
                )

    def test_every_model_said_to_be_about_itself_exists(self):
        for label in ABOUT_THEMSELVES:
            with self.subTest(label=label):
                apps.get_model(label)


class ChainTests(TestCase):
    def setUp(self):
        managing_everything()
        self.first = AuditLog.objects.order_by("-pk").values_list("pk", flat=True).first() or 0

    def written(self):
        return AuditLog.objects.filter(pk__gt=self.first)

    def test_a_record_from_its_declaration_to_its_removal_is_one_read_linked_by_its_causes(self):
        created = execute_capability(
            "infrastructure.resource.create",
            {"key": KEY, "kind": "adguard.rewrite", "spec": dict(REWRITE)},
            principal=cli_principal(),
        )
        self.assertTrue(created["ok"], created)
        applied = claim_and_report()
        reconciled = execute_capability(
            "infrastructure.resource.reconcile",
            {"idempotency_key": "reconcile-once"},
            principal=cli_principal(),
            target=KEY,
        )
        self.assertTrue(reconciled["ok"], reconciled)
        again = claim_and_report()
        removed = execute_capability(
            "infrastructure.resource.remove", {"idempotency_key": "remove-once"}, principal=cli_principal(), target=KEY
        )
        self.assertTrue(removed["ok"], removed)
        gone = claim_and_report()
        self.assertFalse(ManagedResource.objects.filter(key=KEY).exists())

        about = self.written().filter(
            object_type__in=(audited_labels()[ManagedResource], audited_labels()[OperationRequest])
        )
        self.assertEqual(
            set(AuditLog.objects.filter(subject=KEY).values_list("pk", flat=True)),
            set(about.values_list("pk", flat=True)),
        )
        self.assertEqual(
            list(record_timeline(KEY).values_list("pk", flat=True)),
            list(about.order_by("created_at", "pk").values_list("pk", flat=True)),
        )
        requests = [applied, again, gone]
        queued = queue_rows(requests)
        self.assertEqual(set(queued), set(requests))
        asks = {row.operation_id for row in queued.values()}
        self.assertTrue(any(row.operation_id in requests for row in about), "the controller's work is in the story")
        for row in about:
            with self.subTest(row=str(row)):
                if row.operation_id in requests:
                    # The controller's work leads back to whoever queued it.
                    self.assertEqual(row.cause, queued[row.operation_id].operation_id)
                else:
                    self.assertIn(row.operation_id, asks)

    def test_an_agents_held_ask_its_approval_and_the_work_it_queued_are_one_story(self):
        from .test_approvals import GATED_KIND, POLICY_KEY, declare_policy, reconcile_payload

        declare_policy()
        OperationRequest.objects.all().delete()
        held = execute_capability(
            "infrastructure.resource.reconcile", reconcile_payload(), principal=mcp_principal(), target=POLICY_KEY
        )
        self.assertEqual(held["status"], "awaiting_approval", held)
        approval = ApprovalRequest.objects.get()
        operator = get_user_model().objects.create_user(username="operator", password="test-only-password")
        decided = run_capability(
            "approval.approve",
            {"approval_id": str(approval.pk)},
            principal=web_principal(operator),
            idempotency_key="approve-once",
        ).answer
        self.assertTrue(decided["ok"], decided)
        claimed = bridge_client.call("claim", controller_id=CONTROLLER, capability=[f"{GATED_KIND}:reconcile"])[
            "operation"
        ]
        bridge_client.call(
            "report",
            {"success": True, "observed_generation": asked_generation(claimed["id"]), "message": SAID},
            controller_id=CONTROLLER,
            operation=claimed["id"],
        )

        story = list(record_timeline(POLICY_KEY))
        operations = {row.operation_id for row in story}
        self.assertIn(str(approval.pk), operations)
        self.assertIn(claimed["id"], operations)
        self.assertTrue(
            any(row.object_type == audited_labels()[ApprovalRequest] for row in story), "the approval is in the story"
        )
        work = [row for row in story if row.operation_id == claimed["id"]]
        self.assertTrue(work)
        # Queued by the approval, so the controller's work leads back to it.
        self.assertEqual({row.cause for row in work}, {str(approval.pk)})

    def test_rows_written_before_rows_named_their_record_are_still_in_its_story(self):
        resource = ManagedResource.objects.create(key=KEY, kind="adguard.rewrite", spec=dict(REWRITE))
        with operation_context(interface="cli", actor="example-operator", operation="example.older"):
            older = AuditLog.objects.create(
                action=AuditLog.Action.UPDATED,
                object_type=audited_labels()[ManagedResource],
                object_id=str(resource.pk),
                object_repr=KEY,
                operation_id=bound().operation_id,
            )

        self.assertEqual(older.subject, "")
        self.assertIn(older, record_timeline(KEY))


class ControllerBindingTests(TestCase):
    def setUp(self):
        managing_everything()
        created = execute_capability(
            "infrastructure.resource.create",
            {"key": KEY, "kind": "adguard.rewrite", "spec": dict(REWRITE)},
            principal=cli_principal(),
        )
        self.assertTrue(created["ok"], created)
        self.operation = OperationRequest.objects.get()

    def rows(self) -> list[AuditLog]:
        return list(
            AuditLog.objects.filter(
                object_type=audited_labels()[OperationRequest], object_id=str(self.operation.pk)
            ).exclude(action=AuditLog.Action.CREATED)
        )

    def test_a_claim_and_its_report_run_under_the_request_they_carry_out(self):
        claim_and_report()

        rows = self.rows()
        self.assertEqual(len(rows), 2, "the claim and the report each write the request's row")
        self.assertEqual({row.operation_id for row in rows}, {str(self.operation.pk)})

    def test_the_finishing_row_says_what_the_controller_reported(self):
        claim_and_report(success=False)

        finished = AuditLog.objects.filter(
            object_type=audited_labels()[OperationRequest], object_id=str(self.operation.pk)
        ).latest("pk")
        self.assertEqual(finished.message, SAID)


class CauseTests(SimpleTestCase):
    def test_an_operation_started_inside_another_is_caused_by_it(self):
        with operation_context(interface="cli", actor="example-operator", operation="example.outer"):
            outer = bound()
            with operation_context(
                interface="cli", actor="example-operator", operation="example.inner", operation_id="example-inner"
            ):
                inner = bound()
                with operation_context(interface="cli", actor="example-operator", operation="example.step"):
                    step = bound()

        self.assertEqual(inner.cause, outer.operation_id)
        # A step inside an operation is that operation, and keeps its cause.
        self.assertEqual((step.operation_id, step.cause), ("example-inner", outer.operation_id))
        self.assertEqual(outer.cause, "")
