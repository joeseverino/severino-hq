"""Adopting a proposal makes one scanned commit the catalog HQ runs
procedures from, and only a person adopts or rejects one: a commit GitHub did
not verify, or one carrying an Ansible configuration or plugins, is refused;
an agent's adoption is refused, never held, whatever its rule says; what a
person decides on marks each widening and each playbook HQ can't adopt; and
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
from ..automation_adoption import REJECT
from ..capabilities import capability_registry, run_capability
from ..machines import Machine
from ..security import MCP_INTERFACE, Capability, Principal, agent_subject, mcp_principal
from .test_approvals import an_operator
from .test_automation_sources import RefreshTestCase

ADOPT = "automation.source.adopt"
OPERATOR = Principal(
    "example-operator",
    "web",
    frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE, Capability.ADOPT_PLAYBOOKS}),
)
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

    def adopt(
        self, proposal: int, principal: Principal = OPERATOR, key: str = "example-adopt", playbook: str = ""
    ) -> dict[str, Any]:
        named = {"playbook": playbook} if playbook else {}
        with self.captureOnCommitCallbacks(execute=True):
            return run_capability(
                ADOPT, {"proposal": proposal, **named}, principal=principal, idempotency_key=key
            ).answer

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


class AgentAdoptionTests(AdoptionTestCase):
    def setUp(self):
        # An agent whose grant names adopting too, as a token could.
        self.agent = Principal("example-agent", MCP_INTERFACE, OPERATOR.capabilities)

    def test_an_agent_s_adoption_or_rejection_is_refused_and_nothing_is_held_whatever_its_rule_says(self):
        proposal = self.proposed(catalog(procedure("check")))
        for capability in (ADOPT, REJECT):
            CapabilityRule.objects.create(capability=capability, subject=agent_subject(self.agent), rule="allow")
            with self.subTest(capability=capability):
                answer = run_capability(
                    capability, {"proposal": proposal}, principal=self.agent, idempotency_key=f"example-{capability}"
                ).answer

                self.assertEqual(answer["error"]["code"], "forbidden", answer)
        self.assertFalse(ApprovalRequest.objects.exists())
        self.assertEqual(self.adopted(), [])
        from hq.domains.control_plane.models import SourceProposal

        self.assertIsNone(SourceProposal.objects.get(pk=proposal).rejected_at)


class ReviewTests(AdoptionTestCase):
    """What a person decides on: the commit read against the one adopted."""

    def review(self, proposal: int):
        from hq.domains.control_plane.models import SourceProposal

        from ..automation_sources import reviewed

        return reviewed(SourceProposal.objects.get(pk=proposal))

    def test_a_new_playbook_s_rows_say_what_its_command_page_says_and_name_its_inputs(self):
        from ..automation_runs import about, operation_registry

        proposal = self.proposed(catalog(procedure("check", effect="infrastructure_change")))
        (group,) = [group for group in self.review(proposal).preview.groups if group.whole == "added"]
        rows = {row.label: row.after for row in group.rows}
        self.adopt(proposal)
        page = {fact.label: fact.value for fact in about(operation_registry()["example.check"], OPERATOR).facts}

        for label in ("Effect", "Access", "Timeout", "Retry"):
            with self.subTest(label=label):
                self.assertEqual(rows[label], page[label])
        self.assertEqual(rows["Inputs"], "Service", "each input by its name, never its terms")
        self.assertFalse([label for label in rows if label.startswith("Inputs ")], rows)
        self.assertNotIn("Title", rows, "the playbook's row is named by its title already")

    def test_each_widening_is_marked_for_the_person_who_decides(self):
        self.adopt(self.proposed(catalog(procedure("check"))))
        widenings = {
            "a new write": catalog(procedure("check"), procedure("wipe", effect="destructive"), revision=SECOND),
            "a privilege": catalog(procedure("check", effect="infrastructure_change", privilege="example.root")),
            "a file every procedure may read": catalog(
                procedure("check"), changed="group_vars/all.yml", unpinned=[{"path": "group_vars", "kind": "variables"}]
            ),
            "a collection": catalog(procedure("check"), changed="requirements.yml"),
            "a read's files": catalog(procedure("check"), changed="roles/check/tasks/main.yml"),
        }

        # Each at a commit of its own, as a change to any file is.
        for index, (widening, found) in enumerate(widenings.items(), start=2):
            with self.subTest(widening=widening):
                shown = self.review(self.proposed({**found, "revision": str(index) * 40}))

                self.assertTrue(shown.unmarked or any(group.widens for group in shown.preview.groups))
        self.assertEqual(self.adopted(), [FIRST])

    def test_a_playbook_hq_cannot_adopt_is_its_own_row_with_its_problems_and_nothing_else_moves(self):
        self.adopt(self.proposed(catalog(procedure("check"), procedure("other"))))
        broken = procedure("check")
        broken["problems"] = ["Role check_v2 is missing. Add it at roles/check_v2/ in the repository."]
        unbounded = procedure("fresh")
        unbounded["roles"][0]["argument_spec"]["options"]["service"] = {"type": "dict"}

        shown = self.review(self.proposed(catalog(broken, procedure("other"), unbounded, revision=SECOND)))

        groups = {group.label: group for group in shown.preview.groups}
        self.assertEqual([row.label for row in groups["The check procedure"].rows], ["Problems"])
        self.assertTrue(groups["The check procedure"].widens, "its row is marked")
        self.assertEqual(groups["The fresh procedure"].whole, "added")
        self.assertIn("free-form type", " ".join(row.after for row in groups["The fresh procedure"].rows))
        self.assertFalse(groups["The other procedure"].changed)
        self.assertFalse([said for said in shown.unmarked if "can't be adopted" in said])
        self.assertFalse(shown.unplaced, "every reason is on a playbook's row")
        self.assertEqual(len(shown.refused), 2)

    def test_a_widening_names_each_playbook_by_its_title_and_its_id_only_beside_a_shared_title(self):
        from ..automation_adoption import plan
        from ..automation_runs import access_words

        self.adopt(self.proposed(catalog(procedure("check", effect="infrastructure_change"))))
        twin = procedure("twin", effect="destructive")
        twin["title"] = "The check procedure"
        changed = procedure("check", effect="infrastructure_change", privilege="example.root")
        planned = plan(self.proposed(catalog(changed, twin, revision=SECOND)))

        self.assertEqual(
            list(planned.widens),
            [
                f"The check procedure (example.check) gains {access_words('example.root')}.",
                "The check procedure (example.twin) is new, and it stops or deletes.",
            ],
        )


class RejectTests(AdoptionTestCase):
    def reject(self, proposal: int, principal: Principal = OPERATOR, key: str = "example-reject") -> dict[str, Any]:
        return run_capability(REJECT, {"proposal": proposal}, principal=principal, idempotency_key=key).answer

    def test_a_person_rejects_a_scan_once_and_it_waits_no_more(self):
        from hq.domains.control_plane.models import SourceProposal

        from ..automation_sources import standing

        self.adopt(self.proposed(catalog(procedure("check"))))
        newer = self.proposed(catalog(procedure("other"), revision=SECOND))
        self.assertEqual(standing().pending.proposal.pk, newer)

        self.assertTrue(self.reject(newer)["rejected"])
        self.assertFalse(self.reject(newer, key="example-reject-again")["rejected"])

        rejected = SourceProposal.objects.get(pk=newer)
        self.assertEqual(rejected.rejected_by, OPERATOR.actor)
        self.assertIsNone(standing().pending)
        self.assertEqual(self.adopted(), [FIRST])

    def test_the_commit_hq_runs_from_is_not_rejected_and_a_reader_rejects_nothing(self):
        adopted = self.proposed(catalog(procedure("check")))
        self.adopt(adopted)

        self.assertEqual(self.reject(adopted)["error"]["code"], "precondition")
        self.assertEqual(self.reject(adopted, READER, key="example-reader")["error"]["code"], "forbidden")


def sharing(name: str, role: str = "common") -> dict[str, Any]:
    """A procedure whose one role is ``role``, which another procedure may run too."""

    found = procedure(name)
    found["roles"][0]["role"] = role
    return found


class PlaybookByPlaybookTests(AdoptionTestCase):
    """A person adopts or rejects each playbook a scan changes on its own:
    each adopted playbook runs from the commit it was adopted from, and the
    rest stay as adopted."""

    ROLE = "roles/common/tasks/main.yml"

    def reject(self, proposal: int, playbook: str, key: str = "example-reject") -> dict[str, Any]:
        payload = {"proposal": proposal, "playbook": playbook}
        return run_capability(REJECT, payload, principal=OPERATOR, idempotency_key=key).answer

    def pinned(self) -> dict[str, Any]:
        from ..automation_adoption import procedures

        return {found.id: found for found in procedures()}

    def waiting(self, proposal: int) -> dict[str, str]:
        from ..automation_adoption import judged

        return {change.key: change.change for change in judged(proposal).changes}

    def asked_at(self, playbook: str) -> str:
        """The commit a run of ``playbook`` asked now hands the worker."""

        machines = patch("hq.platform.application.automation_runs.machine_catalog", return_value=(Machine(APP),))
        with machines:
            answer = run_capability(
                f"automation.example.{playbook}.run",
                {"hosts": [APP], "parameters": {}},
                principal=OPERATOR,
                idempotency_key=f"example-run-{playbook}",
            ).answer
        self.assertTrue(answer.get("queued"), answer)
        return OperationRequest.objects.get(pk=answer["operation"]["id"]).input["revision"]

    def test_a_shared_role_changed_in_a_newer_commit_reaches_only_the_playbook_adopted_from_it(self):
        from ..automation_runs import operation_registry

        self.adopt(self.proposed(catalog(sharing("check"), sharing("other"))))
        newer = self.proposed(catalog(sharing("check"), sharing("other"), revision=SECOND, changed=self.ROLE))
        self.assertEqual(self.waiting(newer), {"example.check": "changed", "example.other": "changed"})

        answer = self.adopt(newer, key="example-adopt-check", playbook="example.check")

        self.assertTrue(answer["adopted"], answer)
        self.assertTrue(answer["widened"], "its new role files are said")
        self.assertFalse([said for said in answer["widened"] if not said.startswith("The check procedure ")])
        pinned = self.pinned()
        self.assertEqual(
            (pinned["example.check"].revision, pinned["example.check"].files[self.ROLE]), (SECOND, "b" * 40)
        )
        self.assertEqual(
            (pinned["example.other"].revision, pinned["example.other"].files[self.ROLE]), (FIRST, "a" * 40)
        )
        self.assertEqual(operation_registry()["example.check"].revision, SECOND)
        self.assertEqual(
            self.asked_at("other"), FIRST, "the worker checks out the commit the playbook was adopted from"
        )
        self.assertEqual(self.waiting(newer), {"example.other": "changed"})

    def test_a_rejected_playbook_stays_as_adopted_and_adopting_all_leaves_it(self):
        from ..automation_sources import standing

        self.adopt(self.proposed(catalog(sharing("check"), sharing("other"))))
        newer = self.proposed(catalog(sharing("check"), sharing("other"), revision=SECOND, changed=self.ROLE))

        self.assertTrue(self.reject(newer, "example.other")["rejected"])
        self.assertFalse(self.reject(newer, "example.other", key="example-reject-again")["rejected"])
        refused = self.adopt(newer, key="example-adopt-other", playbook="example.other")
        self.assertEqual(refused["error"]["code"], "precondition", refused)

        self.assertEqual(self.waiting(newer), {"example.check": "changed"})
        self.adopt(newer, key="example-adopt-rest")
        self.assertEqual(
            {key: found.revision for key, found in self.pinned().items()},
            {
                "example.check": SECOND,
                "example.other": FIRST,
            },
        )
        self.assertIsNone(standing().pending, "nothing of it waits")

    def test_adopting_a_removed_playbook_removes_it_and_leaves_the_rest(self):
        self.adopt(self.proposed(catalog(procedure("check"), procedure("other"))))
        newer = self.proposed(catalog(procedure("check"), revision=SECOND))
        self.assertEqual(self.waiting(newer), {"example.other": "removed"})

        self.adopt(newer, key="example-adopt-removal", playbook="example.other")

        self.assertEqual({key: found.revision for key, found in self.pinned().items()}, {"example.check": FIRST})
        self.assertNotIn("automation.example.other.run", capability_registry())

    def test_a_playbook_hq_cannot_run_is_refused_alone_and_the_one_beside_it_is_adopted(self):
        self.adopt(self.proposed(catalog(procedure("check"))))
        unbounded = procedure("check")
        unbounded["roles"][0]["argument_spec"]["options"]["service"] = {"type": "dict"}
        newer = self.proposed(catalog(unbounded, procedure("fresh"), revision=SECOND))

        every = self.adopt(newer, key="example-adopt-all")
        alone = self.adopt(newer, key="example-adopt-check", playbook="example.check")
        fresh = self.adopt(newer, key="example-adopt-fresh", playbook="example.fresh")

        self.assertIn("playbooks/check.hq.yml", " ".join(every["error"]["details"]["refused"]))
        self.assertIn("playbooks/check.hq.yml", " ".join(alone["error"]["details"]["refused"]))
        self.assertTrue(fresh["adopted"], fresh)
        self.assertEqual(
            {key: found.revision for key, found in self.pinned().items()},
            {
                "example.check": FIRST,
                "example.fresh": SECOND,
            },
        )

    def test_a_playbook_the_scan_does_not_hold_is_not_found(self):
        newer = self.proposed(catalog(procedure("check")))

        answer = self.adopt(newer, playbook="example.missing")

        self.assertEqual(answer["error"]["code"], "not_found", answer)
        self.assertEqual(self.adopted(), [])


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
