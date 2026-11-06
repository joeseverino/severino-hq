"""A managed record's whole life, in any order Hypothesis finds.

One internal DNS record is declared, applied, changed at AdGuard behind HQ's
back, kept as live, switched off and on, removed and adopted, between sweeps
that list AdGuard, carry it or read another kind. A fake AdGuard holds the
records and a fake controller does what HQ hands it. After each step HQ keeps
the promises below; a broken one prints the shortest list of steps that breaks
it.
"""

from contextlib import suppress
from datetime import timedelta
from unittest import skipUnless

from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, OperationRequest, ProviderInventory

from ..adoption import AdoptCommand, adopt
from ..adoption_testing import managing_everything
from ..controller import ControllerReport, claim_next_operation, report_operation
from ..findings import derive_findings
from ..infrastructure import ManagedResourceCommand, save_managed_resource
from ..inventory import record_identity
from ..record_errors import PolicyError, RecordNotFound
from ..resource_operations import OperationCommand, accept_observed, request_reconcile, request_removal
from ..security import cli_principal
from ..sweep import record_sweep
from ..topology import derive_topology
from .inventory_records import a_sweep

try:
    import time_machine
    from hypothesis import settings
    from hypothesis.stateful import (
        Bundle,
        RuleBasedStateMachine,
        consumes,
        invariant,
        multiple,
        precondition,
        rule,
        run_state_machine_as_test,
    )
    from hypothesis.strategies import sampled_from
except ImportError:  # pragma: no cover - the image does not carry them
    HYPOTHESIS = False
else:
    HYPOTHESIS = True

KIND = "adguard.rewrite"
DOMAINS = ("app.example.com", "tool.example.com", "wiki.example.com")
ANSWERS = ("192.0.2.10", "192.0.2.20")
CONTROLLER = "example-controller"
CARRIED = {"ok": True, "records": [], "carried": True}
FAILED = {"ok": False, "records": [], "error": "unreachable"}
# The controller tries AdGuard on every pass, so a sweep that reads it or
# fails to stands for one pass; one that carries it or reads another kind
# stands for a moment between two passes and takes no time.
READ_EVERY = timedelta(minutes=20)
# The rules a state HQ brought about itself must never raise.
OWN_DOING = ("never-observed", "reconciled-but-still-wrong", "skipped-by-a-sweep", "reporting-a-fault")


def _identity(domain: str) -> tuple[str, ...]:
    return record_identity(KIND, {"domain": domain})


if HYPOTHESIS:

    class RecordLife(RuleBasedStateMachine):
        """HQ and a fake AdGuard, with what HQ promises held between them."""

        declarations = Bundle("declarations")

        def __init__(self):
            super().__init__()
            assert not ManagedResource.objects.exists(), "an example began with another's declarations"
            assert not ProviderInventory.objects.exists(), "an example began with another's sweep"
            self.principal = cli_principal()
            # One savepoint per example, rolled back in teardown, so each starts
            # from the test's own rows. Opened last: Hypothesis tears down only
            # a machine it has, and a savepoint left open would end the test's
            # own transaction in place of it.
            self._clock = time_machine.travel(timezone.now(), tick=False)
            self._moment = self._clock.start()
            self._savepoint = transaction.atomic()
            self._savepoint.__enter__()
            # AdGuard's rewrites, domain to answer, in the order it lists them.
            self.live: dict[str, str] = {}
            # Domains HQ removed at AdGuard that no sweep since has listed.
            self.removed: list[str] = []
            # Domains changed at AdGuard since HQ last read or wrote them.
            self.disturbed: list[str] = []
            self.last_sweep: dict | None = None
            # When a sweep last read the kind with an answer.
            self.answered_at = None
            self.asked = 0

        def teardown(self):
            transaction.set_rollback(True)
            self._savepoint.__exit__(None, None, None)
            self._clock.stop()

        # What HQ is asked to do.

        @rule(target=declarations, domain=sampled_from(DOMAINS), answer=sampled_from(ANSWERS))
        def create(self, domain, answer):
            if self._declared(domain):
                return multiple()
            key = self._ask("declared")
            save_managed_resource(
                ManagedResourceCommand(key=key, kind=KIND, spec={"domain": domain, "answer": answer}),
                principal=self.principal,
            )
            self._forget_removed(domain)
            return key

        @rule(key=declarations)
        def apply(self, key):
            if ManagedResource.objects.get(key=key).enabled:
                request_reconcile(
                    OperationCommand(idempotency_key=self._ask("apply")), principal=self.principal, current_key=key
                )
            self._controller("reconcile")

        @rule(key=declarations)
        def keep_what_is_live(self, key):
            # Refused when no sweep has listed it: there is nothing live to keep.
            with suppress(RecordNotFound):
                accept_observed(
                    OperationCommand(idempotency_key=self._ask("accept")), principal=self.principal, current_key=key
                )

        @rule(key=declarations)
        def switch_off(self, key):
            self._switch(key, enabled=False)

        @rule(key=declarations)
        def switch_on(self, key):
            self._switch(key, enabled=True)

        @rule(key=consumes(declarations))
        def remove(self, key):
            request_removal(
                OperationCommand(idempotency_key=self._ask("remove")), principal=self.principal, current_key=key
            )
            self._controller("delete")
            assert not ManagedResource.objects.filter(key=key).exists(), "a confirmed removal kept its declaration"

        @rule(target=declarations, domain=sampled_from(DOMAINS))
        def adopt_by_hand(self, domain):
            try:
                saved = adopt(AdoptCommand(kind=KIND, hostname=domain), principal=self.principal)
            except RecordNotFound, PolicyError:
                return multiple()
            return saved["resource"]["key"]

        # What happens at AdGuard without HQ.

        @rule(domain=sampled_from(DOMAINS), answer=sampled_from(ANSWERS))
        def changed_at_adguard(self, domain, answer):
            self.live[domain] = answer
            self._disturb(domain)

        @rule(domain=sampled_from(DOMAINS))
        def deleted_at_adguard(self, domain):
            self.live.pop(domain, None)
            self._disturb(domain)

        # What the controller reads.

        @rule(target=declarations)
        def sweep(self):
            payload = a_sweep(**{KIND: [self._record(domain, answer) for domain, answer in self.live.items()]})
            self.disturbed = []
            self._moment.shift(READ_EVERY)
            return self._sweep(payload)

        @rule(target=declarations)
        def sweep_failing_read(self):
            self._moment.shift(READ_EVERY)
            return self._sweep({KIND: FAILED})

        @rule(target=declarations)
        def sweep_carrying_it(self):
            return self._sweep({KIND: CARRIED})

        @rule(target=declarations)
        def sweep_of_another_kind(self):
            return self._sweep(a_sweep(**{"npm.proxy_host": []}))

        @precondition(lambda self: self.last_sweep is not None)
        @rule(target=declarations)
        def the_same_sweep_again(self):
            before = self._declared_state()
            adopted = self._sweep(self.last_sweep)
            after = self._declared_state()
            assert {key: after.get(key) for key in before} == before, (
                f"a sweep that repeats the last one changed a declaration: {before} became {after}"
            )
            return adopted

        @rule()
        def look_at_findings(self):
            settled = self._settled()
            topology = derive_topology(principal=self.principal)
            raised = [
                (finding.rule, finding.subject)
                for finding in derive_findings(topology, principal=self.principal)
                if finding.rule in OWN_DOING and finding.subject in settled
            ]
            assert not raised, f"HQ raised a finding about a record it settled itself: {raised}"
            read = dict(topology.kinds_read).get(KIND)
            assert read == self.answered_at, (
                f"the kind's read moment is {read}, its last answered read {self.answered_at}"
            )

        # The promises.

        # One invariant rather than one per promise: Hypothesis prints each
        # invariant after every step of a failing case.
        @invariant()
        def promises_hold(self):
            identities = [record_identity(KIND, spec) for spec in self._specs()]
            back = [domain for domain in self.removed if _identity(domain) in identities]
            assert not back, f"HQ declared again a record it removed: {back}"
            twice = sorted({identity for identity in identities if identities.count(identity) > 1})
            assert not twice, f"two declarations hold one record: {twice}"

        # The fakes.

        def _controller(self, action):
            """Claim and complete every queued ``action``, as the controller does."""

            while (claim := claim_next_operation(CONTROLLER, capabilities=((KIND, action),)))["operation"]:
                operation = OperationRequest.objects.select_related("resource").get(pk=claim["operation"]["id"])
                spec = claim["resource"]["spec"]
                domain = spec["domain"]
                if action == "reconcile":
                    assert claim["resource"]["enabled"], f"HQ handed its controller a change to switched-off {domain}"
                    self.live[domain] = spec["answer"]
                    status = self._record(domain, spec["answer"])
                    reason, said = "Reconciled", "The AdGuard record is as HQ set it."
                else:
                    reason = "Removed" if self.live.pop(domain, None) is not None else "Absent"
                    status, said = {"domain": domain, "removed": True}, ""
                    self.removed.append(domain)
                self._forget_disturbed(domain)
                report_operation(
                    str(operation.pk),
                    ControllerReport(
                        success=True,
                        observed_generation=operation.input["generation"],
                        status=status,
                        conditions=[{"type": "Ready", "status": True, "reason": reason, "message": said}],
                    ),
                    controller_id=CONTROLLER,
                )

        def _sweep(self, payload):
            # A read that lists a record HQ removed is AdGuard's word that it is back.
            listed = [record["domain"] for record in payload.get(KIND, {}).get("records", ())]
            self.removed = [domain for domain in self.removed if domain not in listed]
            self.last_sweep = payload
            report = payload.get(KIND, {})
            if report.get("ok") and not report.get("carried"):
                self.answered_at = timezone.now()
            adopted = record_sweep(payload, principal=self.principal)["adopted"]
            return multiple(*adopted)

        @staticmethod
        def _record(domain, answer):
            return {"domain": domain, "answer": answer, "enabled": True}

        def _switch(self, key, *, enabled):
            resource = ManagedResource.objects.get(key=key)
            save_managed_resource(
                ManagedResourceCommand(key=key, kind=KIND, spec=resource.spec, enabled=enabled),
                principal=self.principal,
                current_key=key,
                sent=frozenset({"enabled"}),
            )

        def _ask(self, verb):
            self.asked += 1
            return f"{verb}-{self.asked}"

        def _disturb(self, domain):
            if domain not in self.disturbed:
                self.disturbed.append(domain)

        def _forget_disturbed(self, domain):
            self.disturbed = [other for other in self.disturbed if other != domain]

        def _forget_removed(self, domain):
            self.removed = [other for other in self.removed if other != domain]

        def _specs(self):
            return list(ManagedResource.objects.filter(kind=KIND).order_by("pk").values_list("spec", flat=True))

        def _declared(self, domain):
            return _identity(domain) in [record_identity(KIND, spec) for spec in self._specs()]

        def _declared_state(self):
            return {
                key: (spec, enabled, generation)
                for key, spec, enabled, generation in ManagedResource.objects.filter(kind=KIND).values_list(
                    "key", "spec", "enabled", "generation"
                )
            }

        def _settled(self):
            """The declarations whose state HQ brought about and has seen: on,
            applied as declared, matching AdGuard, and untouched there since."""

            return [
                f"resource:{resource.key}"
                for resource in ManagedResource.objects.filter(kind=KIND, enabled=True)
                if resource.last_observed_at
                and resource.observed_generation == resource.generation
                and self.live.get(resource.spec["domain"]) == resource.spec["answer"]
                and resource.spec["domain"] not in self.disturbed
            ]


@skipUnless(HYPOTHESIS, "hypothesis is a development tool")
class RecordLifeTests(TestCase):
    def setUp(self):
        super().setUp()
        managing_everything()

    def test_every_order_of_a_record_s_life_keeps_hqs_promises(self):
        run_state_machine_as_test(RecordLife, settings=settings())

    def test_applying_one_record_while_its_kind_cannot_be_read_leaves_the_others_settled(self):
        """The shortest order the machine found: HQ's own apply moved no read."""

        machine = RecordLife()
        try:
            machine.changed_at_adguard("app.example.com", "192.0.2.10")
            machine.sweep()
            for _ in range(4):
                machine.sweep_failing_read()
            machine.apply(machine.create("tool.example.com", "192.0.2.20"))
            machine.look_at_findings()
        finally:
            machine.teardown()

    def test_a_kind_s_read_moment_is_only_ever_a_read_that_answered(self):
        """A failed first read is no read; one after an empty read keeps that read's moment."""

        machine = RecordLife()
        try:
            machine.sweep_failing_read()
            machine.look_at_findings()
            machine.sweep()
            machine.sweep_failing_read()
            machine.look_at_findings()
        finally:
            machine.teardown()
