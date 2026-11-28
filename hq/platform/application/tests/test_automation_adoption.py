"""Adopting a proposal makes one scanned commit the catalog HQ runs
procedures from, and only a person adopts or rejects one: a commit GitHub did
not verify, or one carrying an Ansible configuration or plugins, is refused;
an agent's adoption is refused, never held, whatever its rule says; what a
person decides on marks each widening and each playbook HQ can't adopt; and
every process's commands follow the catalog without a restart.

Each catalog is in the bridge contract's ``SourceCatalog`` shape and arrives
the way a scan's does: HQ's ask, the worker's claim, and its report.
"""

from contextlib import suppress
from copy import deepcopy
from datetime import timedelta
from typing import Any
from unittest.mock import patch

from django.utils import timezone

from hq.domains.control_plane.models import ApprovalRequest, OperationRequest

from ..approvals import ApprovalError, ApproveCommand, approve
from ..automation_adoption import REJECT
from ..automation_sources import queue_scan
from ..capabilities import capability_spec, run_capability
from ..machines import Machine
from ..security import MCP_INTERFACE, Capability, Principal, internal_principal, mcp_principal
from .test_approvals import an_operator
from .test_automation_sources import DOORBELL, SOURCE, ScanTestCase

ADOPT = "automation.source.adopt"
OPERATOR = Principal(
    "example-operator",
    "web",
    frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE, Capability.ADOPT_PLAYBOOKS}),
)
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
        "repository": SOURCE,
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


class AdoptionTestCase(ScanTestCase):
    scans = 0

    def proposed(self, found: dict[str, Any]) -> int:
        """The proposal a scan reporting ``found`` made."""

        type(self).scans += 1
        with patch(DOORBELL), self.captureOnCommitCallbacks(execute=True):
            queue_scan(principal=internal_principal("controller"), once=f"example-scan-{self.scans}")
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
        self.assertIsNone(capability_spec("automation.example.check.run"))

        answer = self.adopt(proposal)

        self.assertTrue(answer["adopted"], answer)
        self.assertEqual(self.adopted(), [FIRST])
        self.assertIsNotNone(capability_spec("automation.example.check.run"))

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

    def test_an_input_that_says_nothing_of_what_it_is_refuses_the_commit(self):
        unsaid = procedure("check")
        unsaid["roles"][0]["argument_spec"]["options"] = {"cache": {"type": "str", "choices": ["web"]}}

        self.assertIn("cache says nothing", self.refused(self.adopt(self.proposed(catalog(unsaid)))))

    def test_a_sidecar_names_only_a_reading_hq_declares_and_its_command_says_which(self):
        unknown = procedure("check", readings=["example.unknown_reading"])

        said = self.refused(self.adopt(self.proposed(catalog(unknown)), key="example-adopt-unknown"))

        self.assertIn("example.unknown_reading", said)
        self.assertTrue(self.adopt(self.proposed(catalog(procedure("check", readings=["apps.pull"]))))["adopted"])
        notes = " ".join(capability_spec("automation.example.check.run").execution_notes)
        self.assertIn("`apps.pull`", notes)

    def test_a_change_is_settled_and_verified_only_by_a_read_of_its_targets_at_its_revision(self):
        from ..automation_runs import operation_registry

        def at_revision(found: dict[str, Any], **terms: Any) -> dict[str, Any]:
            found["declaration"]["inputs"] = {"revision": {"pattern": "^[0-9a-f]{40}$", "max_length": 40}}
            found["roles"][0]["argument_spec"]["options"] = {
                "revision": {"type": "str", "required": True, "description": "The commit.", **terms}
            }
            return found

        # Its play asserts nothing once it hands off: the read that settles it does.
        apply = at_revision(procedure("apply", effect="infrastructure_change", settled_by="example.last"))
        apply["verification"] = []
        # The read may also be run with no revision, which it says in words of its own.
        last = at_revision(procedure("last"), required=False, default="", description="The pull's commit.")
        silent = deepcopy(last)
        last["verification"] = ["Every app is proven at the revision."]
        cases = {
            "a read of this commit": procedure("other"),
            "a read": at_revision(procedure("last", effect="infrastructure_change")),
            "the same kinds of target": at_revision(procedure("last", target_kinds=["example.edge"])),
            "its revision input": procedure("last"),
            "a read that asserts": silent,
        }
        for number, (case, read) in enumerate(cases.items()):
            with self.subTest(case):
                found = self.proposed(catalog(apply, read))

                said = self.refused(self.adopt(found, key=f"example-settle-{number}", playbook="example.apply"))
                self.assertIn("settled_by", said)
        self.assertTrue(self.adopt(self.proposed(catalog(apply, last)))["adopted"])
        self.assertEqual(operation_registry()["example.apply"].verification, ("Every app is proven at the revision.",))

    def test_a_procedure_s_inputs_are_its_role_s_argument_spec_bounded_by_its_sidecar(self):
        bounded = procedure("check", inputs={"unit": {"pattern": "^[a-z]+$", "max_length": 8}})
        bounded["roles"][0]["argument_spec"]["options"] = {
            "unit": {"type": "str", "required": True, "description": "The unit to check."}
        }
        self.adopt(self.proposed(catalog(bounded)))
        machines = patch("hq.platform.application.playbook_reach.machine_catalog", return_value=(Machine(APP),))

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
        self.assertIsNotNone(capability_spec("automation.example.check.run"))

        self.adopt(self.proposed(catalog(procedure("other"), revision=SECOND)), key="example-adopt-2")

        self.assertIsNone(capability_spec("automation.example.check.run"))
        self.assertIsNotNone(capability_spec("automation.example.other.run"))

    def test_a_process_that_did_not_adopt_catches_up_at_its_next_request(self):
        from hq.platform.application.automation_adoption import forget, served

        self.adopt(self.proposed(catalog(procedure("check"))))
        # A process that read the catalog before another process adopted this one.
        forget()
        self.assertIsNone(capability_spec("automation.example.check.run"))

        with served():
            self.assertIsNotNone(capability_spec("automation.example.check.run"))

    def test_a_command_the_catalog_does_not_hold_is_named_without_reading_the_catalog(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from hq.domains.control_plane.models import AdoptedCatalog

        from ..automation_adoption import ADOPT, forget, served
        from ..capabilities import capability_title, capability_words, command_spec

        name = "automation.example.check.run"
        self.adopt(self.proposed(catalog(procedure("check"))))
        # A process that read the catalog before another process adopted this one.
        forget()
        with served(), CaptureQueriesContext(connection) as sent:
            words = capability_words(ADOPT)
        self.assertFalse([query for query in sent if AdoptedCatalog._meta.db_table in query["sql"]])
        self.assertEqual(words, command_spec(ADOPT).words(""))

        # A procedure is still read at the catalog adopted now.
        forget()
        with served():
            self.assertEqual(capability_title(name), command_spec(name).title)

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
        super().setUp()
        # An agent whose grant names adopting too, as a token could.
        self.agent = Principal("example-agent", MCP_INTERFACE, OPERATOR.capabilities)


class ReviewTests(AdoptionTestCase):
    """What a person decides on: the commit read against the one adopted."""

    def review(self, proposal: int):
        from hq.domains.control_plane.models import SourceProposal

        from ..automation_sources import reviewed

        return reviewed(SourceProposal.objects.get(pk=proposal))

    def test_a_new_playbook_s_rows_say_what_its_command_page_says_and_name_its_inputs(self):
        from ..automation_runs import operation_registry
        from ..playbook_about import about

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
        from ..automation_sources import standing

        self.adopt(self.proposed(catalog(procedure("check"))))
        newer = self.proposed(catalog(procedure("other"), revision=SECOND))
        self.assertEqual(standing().pending.proposal.pk, newer)

        self.assertTrue(self.reject(newer)["rejected"])
        self.assertFalse(self.reject(newer, key="example-reject-again")["rejected"])

        self.assertIsNone(standing().pending)
        self.assertEqual(self.adopted(), [FIRST])

    def test_the_commit_hq_runs_from_is_not_rejected(self):
        adopted = self.proposed(catalog(procedure("check")))
        self.adopt(adopted)

        self.assertEqual(self.reject(adopted)["error"]["code"], "precondition")


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

        machines = patch("hq.platform.application.playbook_reach.machine_catalog", return_value=(Machine(APP),))
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
        from hq.domains.control_plane.models import ManagedResource

        def records() -> set[str]:
            return set(ManagedResource.objects.filter(kind="automation.operation").values_list("key", flat=True))

        self.adopt(self.proposed(catalog(procedure("check"), procedure("other"))))
        # Each adopted playbook has its record before anything runs it.
        self.assertEqual(records(), {"automation-example-check", "automation-example-other"})
        newer = self.proposed(catalog(procedure("check"), revision=SECOND))
        self.assertEqual(self.waiting(newer), {"example.other": "removed"})

        self.adopt(newer, key="example-adopt-removal", playbook="example.other")

        self.assertEqual({key: found.revision for key, found in self.pinned().items()}, {"example.check": FIRST})
        self.assertIsNone(capability_spec("automation.example.other.run"))
        self.assertEqual(records(), {"automation-example-check"})

    def test_a_playbook_hq_cannot_run_is_refused_alone_and_adopting_all_adopts_the_ones_beside_it(self):
        self.adopt(self.proposed(catalog(procedure("check"))))
        unbounded = procedure("check")
        unbounded["roles"][0]["argument_spec"]["options"]["service"] = {"type": "dict"}
        newer = self.proposed(catalog(unbounded, procedure("fresh"), procedure("other"), revision=SECOND))

        alone = self.adopt(newer, key="example-adopt-check", playbook="example.check")
        every = self.adopt(newer, key="example-adopt-all")

        self.assertIn("playbooks/check.hq.yml", " ".join(alone["error"]["details"]["refused"]))
        self.assertTrue(every["adopted"], every)
        self.assertEqual(
            {key: found.revision for key, found in self.pinned().items()},
            {"example.check": FIRST, "example.fresh": SECOND, "example.other": SECOND},
        )
        self.assertEqual(self.waiting(newer), {"example.check": "changed"}, "the one HQ can't adopt still waits")
        refused = self.adopt(newer, key="example-adopt-all-again")
        self.assertIn("playbooks/check.hq.yml", " ".join(refused["error"]["details"]["refused"]))

    def test_a_playbook_the_scan_does_not_hold_is_not_found(self):
        newer = self.proposed(catalog(procedure("check")))

        answer = self.adopt(newer, playbook="example.missing")

        self.assertEqual(answer["error"]["code"], "not_found", answer)
        self.assertEqual(self.adopted(), [])


class StaleRunTests(AdoptionTestCase):
    def test_a_held_run_goes_stale_when_its_procedure_is_adopted_anew(self):
        wipe = procedure("wipe", effect="destructive")
        self.adopt(self.proposed(catalog(wipe)))
        machines = patch("hq.platform.application.playbook_reach.machine_catalog", return_value=(Machine(APP),))
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
