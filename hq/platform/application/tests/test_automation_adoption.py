"""Adopting a proposal makes one scanned commit the catalog HQ runs
procedures from, once a person agrees: a commit GitHub did not verify, or one
carrying an Ansible configuration or plugins, is refused; an agent's adoption
waits for a person, and a widening waits whatever the agent's rule says; and
every process's commands follow the catalog without a restart.

Each catalog is in the bridge contract's ``SourceCatalog`` shape and arrives
the way a scan's does: a Refresh, the worker's claim, and its report.
"""

from contextlib import suppress
from datetime import timedelta
from typing import Any
from unittest.mock import patch

from django.utils import timezone

from hq.domains.control_plane.models import ApprovalRequest, CapabilityRule, OperationRequest

from ..approvals import ApprovalError, ApproveCommand, approve
from ..capabilities import capability_registry, run_capability
from ..machines import Machine
from ..security import Capability, Principal, agent_subject, mcp_principal
from .test_approvals import an_operator
from .test_automation_sources import RefreshTestCase

ADOPT = "automation.source.adopt"
OPERATOR = Principal("example-operator", "web", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
READER = Principal("example-reader", "web", frozenset({Capability.READ}))
FIRST, SECOND = "1" * 40, "2" * 40
APP = "app.example.com"


def procedure(name: str, *, effect: str = "read", privilege: str | None = None, **declared: Any) -> dict[str, Any]:
    """One procedure as the worker's scan reports it: a sidecar, its playbook and its role's argument spec."""

    writes = effect != "read"
    return {
        "sidecar": f"playbooks/{name}.hq.yml",
        "playbook": f"playbooks/{name}.yml",
        "declaration": {
            "id": f"example.{name}",
            "target_kinds": ["machine"],
            "effect": effect,
            "timeout_seconds": 60,
            "retry": "never" if writes else "safe",
            "recovery": ["Look at the machine."],
            **({"privilege": privilege} if privilege else {}),
            **declared,
        },
        "title": f"The {name} procedure",
        "roles": [
            {
                "role": name,
                "argument_spec": {
                    "options": {
                        "service": {
                            "type": "str",
                            "choices": ["web", "db"],
                            "default": "web",
                            "description": "The service.",
                        }
                    }
                },
            }
        ],
        "preconditions": ["The service is installed."] if writes else [],
        "verification": ["The service answers."] if writes else [],
        "problems": [],
    }


def catalog(*procedures: dict[str, Any], revision: str = FIRST, changed: str = "", **fields: Any) -> dict[str, Any]:
    """A commit holding ``procedures``, each file named by an object id; ``changed`` is a file whose content moved."""

    paths = sorted(
        {
            *(path for found in procedures for path in (found["sidecar"], found["playbook"])),
            *(f"roles/{role['role']}/tasks/main.yml" for found in procedures for role in found["roles"]),
            *([changed] if changed else []),
        }
    )
    return {
        "branch": "main",
        "revision": revision,
        "verification": {"verified": True, "reason": "valid", "committer": "example-maintainer"},
        "procedures": list(procedures),
        "files": [{"path": path, "object": ("b" if path == changed else "a") * 40} for path in paths],
        "unpinned": [],
        # The worker logs in to the one machine as each profile its procedures run as.
        "reaches": [
            {"machine": APP, "profile": (found["declaration"] or {}).get("privilege") or "read"} for found in procedures
        ],
        **fields,
    }


def decided(held: ApprovalRequest) -> None:
    """A person's approval of ``held``, which may find it stale."""

    with suppress(ApprovalError):
        approve(ApproveCommand(str(held.id)), principal=an_operator())


class AdoptionTestCase(RefreshTestCase):
    scans = 0

    def proposed(self, found: dict[str, Any]) -> int:
        """The proposal a scan reporting ``found`` made."""

        type(self).scans += 1
        self.refresh(key=f"example-refresh-{self.scans}")
        claimed = self.claim()
        answer = self.report(
            claimed["operation"]["id"],
            claimed["resource"]["generation"],
            success=True,
            message="Found the procedures.",
            catalog=found,
        )
        self.assertEqual(answer.status, 200, answer.body)
        return OperationRequest.objects.get(pk=claimed["operation"]["id"]).result["proposal"]

    def adopt(self, proposal: int, principal: Principal = OPERATOR, key: str = "example-adopt") -> dict[str, Any]:
        with self.captureOnCommitCallbacks(execute=True):
            return run_capability(ADOPT, {"proposal": proposal}, principal=principal, idempotency_key=key).answer

    def adopted(self) -> list[str]:
        from hq.domains.control_plane.models import AdoptedCatalog

        return list(AdoptedCatalog.objects.values_list("revision", flat=True))

    def refused(self, answer: dict[str, Any]) -> str:
        self.assertFalse(answer["ok"], answer)
        self.assertEqual(self.adopted(), [])
        return " ".join(answer["error"]["details"]["refused"])


class AdoptTests(AdoptionTestCase):
    def test_a_person_adopts_a_verified_proposal_and_its_procedures_become_commands(self):
        proposal = self.proposed(catalog(procedure("check")))
        self.assertNotIn("automation.example.check.run", capability_registry())

        answer = self.adopt(proposal)

        self.assertTrue(answer["adopted"], answer)
        self.assertEqual(self.adopted(), [FIRST])
        self.assertIn("automation.example.check.run", capability_registry())

    def test_someone_who_cannot_manage_infrastructure_is_refused(self):
        proposal = self.proposed(catalog(procedure("check")))

        answer = self.adopt(proposal, READER)

        self.assertEqual(answer["error"]["code"], "forbidden", answer)
        self.assertEqual(self.adopted(), [])

    def test_a_commit_github_did_not_verify_is_refused(self):
        unsigned = {"verified": False, "reason": "unsigned", "committer": ""}

        said = self.refused(self.adopt(self.proposed(catalog(procedure("check"), verification=unsigned))))

        self.assertIn("unsigned", said)

    def test_a_commit_carrying_a_plugin_directory_anywhere_or_a_configuration_is_refused(self):
        for path, kind in (("roles/check/library", "plugins"), ("ansible.cfg", "config")):
            with self.subTest(path=path):
                found = catalog(procedure("check"), unpinned=[{"path": path, "kind": kind}])

                self.assertIn(path, self.refused(self.adopt(self.proposed(found), key=f"example-adopt-{kind}")))

    def test_a_procedure_hq_cannot_run_refuses_the_whole_commit(self):
        unbounded = procedure("check")
        unbounded["roles"][0]["argument_spec"]["options"]["service"] = {"type": "str", "required": True}
        free = procedure("other")
        free["roles"][0]["argument_spec"]["options"]["service"] = {"type": "dict"}

        said = self.refused(self.adopt(self.proposed(catalog(unbounded, free))))

        self.assertIn("playbooks/check.hq.yml", said)
        self.assertIn("playbooks/other.hq.yml", said)

    def test_a_procedure_s_inputs_are_its_role_s_argument_spec_bounded_by_its_sidecar(self):
        bounded = procedure("check", inputs={"unit": {"pattern": "^[a-z]+$", "max_length": 8}})
        bounded["roles"][0]["argument_spec"]["options"] = {"unit": {"type": "str", "required": True}}
        self.adopt(self.proposed(catalog(bounded)))
        machines = patch("hq.platform.application.automation_runs.machine_catalog", return_value=(Machine(APP),))

        with machines:
            refused = run_capability(
                "automation.example.check.run",
                {"hosts": [APP], "parameters": {"unit": "Not-A-Unit"}},
                principal=OPERATOR,
            ).answer
            queued = run_capability(
                "automation.example.check.run", {"hosts": [APP], "parameters": {"unit": "web"}}, principal=OPERATOR
            ).answer

        self.assertEqual(refused["error"]["code"], "invalid_input", refused)
        self.assertTrue(queued.get("queued"), queued)


class RegistryTests(AdoptionTestCase):
    def test_another_adoption_changes_the_commands_without_a_restart(self):
        self.adopt(self.proposed(catalog(procedure("check"))))
        self.assertIn("automation.example.check.run", capability_registry())

        self.adopt(self.proposed(catalog(procedure("other"), revision=SECOND)), key="example-adopt-2")

        commands = capability_registry()
        self.assertNotIn("automation.example.check.run", commands)
        self.assertIn("automation.example.other.run", commands)

    def test_a_process_that_did_not_adopt_catches_up_at_its_next_request(self):
        from hq.platform.application.automation_adoption import forget, served

        self.adopt(self.proposed(catalog(procedure("check"))))
        # A process that read the catalog before another process adopted this one.
        forget()
        self.assertNotIn("automation.example.check.run", capability_registry())

        with served():
            self.assertIn("automation.example.check.run", capability_registry())

    def test_adopting_tells_every_listening_agent_its_tools_changed(self):
        from ..agent_registry import TOOLS_CHANGED

        heard: list[bool] = []

        def listening(*, aimed, **_):
            heard.append(aimed(mcp_principal()))

        TOOLS_CHANGED.connect(listening)
        self.addCleanup(TOOLS_CHANGED.disconnect, listening)

        self.adopt(self.proposed(catalog(procedure("check"))))

        self.assertEqual(heard, [True])


class HeldAdoptionTests(AdoptionTestCase):
    def setUp(self):
        self.agent = mcp_principal()

    def held(self, proposal: int, key: str = "example-agent-adopt") -> ApprovalRequest:
        answer = self.adopt(proposal, self.agent, key=key)
        self.assertEqual(answer.get("status"), "awaiting_approval", answer)
        return ApprovalRequest.objects.get(pk=answer["approval"]["id"])

    def allowed(self) -> None:
        CapabilityRule.objects.create(capability=ADOPT, subject=agent_subject(self.agent), rule="allow")

    def test_an_agent_s_adoption_waits_for_a_person_who_sees_what_it_changes(self):
        from ..approval_review import preview

        held = self.held(self.proposed(catalog(procedure("check"))))

        self.assertEqual(self.adopted(), [])
        self.assertEqual(held.baseline["committer"], "example-maintainer")
        shown = {row.label: row for row in preview(held).changes}
        # Each playbook is read by its title.
        self.assertTrue(
            any("The check procedure" in label and row.change == "added" for label, row in shown.items()), shown
        )

        approve(ApproveCommand(str(held.id)), principal=an_operator())

        self.assertEqual(self.adopted(), [FIRST])

    def test_a_narrow_adoption_runs_under_the_agent_s_rule(self):
        self.allowed()

        answer = self.adopt(self.proposed(catalog(procedure("check"))), self.agent)

        self.assertTrue(answer.get("adopted"), answer)

    def test_a_widening_waits_for_a_person_whatever_the_agent_s_rule_says(self):
        from ..approval_review import review

        self.allowed()
        self.adopt(self.proposed(catalog(procedure("check"))), self.agent)
        widenings = {
            "a new write": catalog(procedure("check"), procedure("wipe", effect="destructive"), revision=SECOND),
            "a privilege": catalog(procedure("check", effect="infrastructure_change", privilege="example.root")),
            "a file every procedure may read": catalog(
                procedure("check"), changed="group_vars/all.yml", unpinned=[{"path": "group_vars", "kind": "variables"}]
            ),
            "a collection": catalog(procedure("check"), changed="requirements.yml"),
            "a read's files": catalog(procedure("check"), changed="roles/check/tasks/main.yml"),
        }

        for index, (widening, found) in enumerate(widenings.items()):
            with self.subTest(widening=widening):
                held = self.held(self.proposed(found), key=f"example-agent-adopt-{index}")

                self.assertTrue(review(held)["warnings"], "the person deciding is warned of what it widens")
        self.assertEqual(self.adopted(), [FIRST])

    def test_a_widening_names_each_playbook_by_its_title_and_its_id_only_beside_a_shared_title(self):
        from ..automation_adoption import plan

        self.adopt(self.proposed(catalog(procedure("check", effect="infrastructure_change"))))
        twin = procedure("twin", effect="destructive")
        twin["title"] = "The check procedure"
        changed = procedure("check", effect="infrastructure_change", privilege="example.root")
        planned = plan(self.proposed(catalog(changed, twin, revision=SECOND)))

        self.assertEqual(
            list(planned.widens),
            [
                "The check procedure (example.check) gains the privilege example.root.",
                "The check procedure (example.twin) is new, and it stops or deletes.",
            ],
        )

    def test_a_held_adoption_goes_stale_when_another_is_adopted_first(self):
        held = self.held(self.proposed(catalog(procedure("check"))))

        self.adopt(self.proposed(catalog(procedure("other"), revision=SECOND)))
        decided(held)

        held.refresh_from_db()
        self.assertEqual(held.state, ApprovalRequest.State.STALE)
        self.assertEqual(self.adopted(), [SECOND])


class StaleRunTests(AdoptionTestCase):
    def test_a_held_run_goes_stale_when_its_procedure_is_adopted_anew(self):
        wipe = procedure("wipe", effect="destructive")
        self.adopt(self.proposed(catalog(wipe)))
        machines = patch("hq.platform.application.automation_runs.machine_catalog", return_value=(Machine(APP),))
        with machines:
            answer = run_capability(
                "automation.example.wipe.run",
                {"hosts": [APP], "parameters": {}},
                principal=mcp_principal(),
                idempotency_key="example-wipe",
            ).answer
            self.assertEqual(answer.get("status"), "awaiting_approval", answer)
            held = ApprovalRequest.objects.get(pk=answer["approval"]["id"])

            self.adopt(
                self.proposed(catalog(wipe, revision=SECOND, changed="roles/wipe/tasks/main.yml")),
                key="example-adopt-2",
            )
            decided(held)

        held.refresh_from_db()
        self.assertEqual(held.state, ApprovalRequest.State.STALE)
        self.assertFalse(OperationRequest.objects.filter(action=OperationRequest.Action.RUN).exists())


class RetentionTests(AdoptionTestCase):
    def test_old_proposals_go_past_the_routine_window_but_the_newest_and_the_adopted_stay(self):
        from hq.domains.control_plane.models import SourceProposal

        from ..automation_adoption import prune_proposals

        adopted = self.proposed(catalog(procedure("check")))
        self.adopt(adopted)
        old = self.proposed(catalog(procedure("other"), revision=SECOND))
        newest = self.proposed(catalog(procedure("check"), revision="3" * 40))
        SourceProposal.objects.update(created_at=timezone.now() - timedelta(days=400))

        prune_proposals()

        self.assertEqual(set(SourceProposal.objects.values_list("pk", flat=True)), {adopted, newest})
        self.assertNotIn(old, SourceProposal.objects.values_list("pk", flat=True))
