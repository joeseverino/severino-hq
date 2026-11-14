"""Synthetic ``example.*`` procedures and machines, supplied as an adopted catalog and HQ's facts would be."""

from typing import Annotated, Any
from unittest.mock import patch

from django.test import TestCase
from pydantic import BaseModel, ConfigDict, Field

from ..automation_manifest import OperationManifest
from ..cadence import note_automation
from ..machines import Machine
from ..plugins import clear_plugin_composition_cache

REVISION = "0123456789abcdef0123456789abcdef01234567"
NEXT_REVISION = "89abcdef0123456789abcdef0123456789abcdef"
RESTART = "example.service_restart"
RESTART_RUN = f"automation.{RESTART}.run"
RESTART_KEY = "automation-example-service_restart"
APP, DB, EDGE = "app.example.com", "db.example.com", "edge.example.com"


class ServiceRestart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: Annotated[str, Field(min_length=1, max_length=60, description="The service to restart.")]


def manifest(**fields: Any) -> OperationManifest:
    return OperationManifest(
        **{
            "id": RESTART,
            "revision": REVISION,
            "playbook": "playbooks/service_restart.yml",
            "input_type": ServiceRestart,
            "target_kinds": ("machine",),
            "privilege": None,
            "effect": "infrastructure_change",
            "timeout_seconds": 300,
            "preconditions": ("The service is installed on the machine.",),
            "verification": ("The service answers its health check.",),
            "recovery": ("Start the service by hand if it stays down.",),
            "retry": "observe_first",
            **fields,
        }
    )


class Supply:
    """Procedures and machines supplied to one test, changeable mid-test.

    The machines replace ``machine_catalog``'s reading of HQ's facts, which a
    run resolves its hosts through, and the worker reaches each of them as
    every profile the manifests name, unless ``reaches`` says otherwise. The
    worker has just arrived, so a run asked for is taken.
    """

    def __init__(self, test: TestCase, *manifests: OperationManifest) -> None:
        # Only a test that may write has a worker to note; one that may not runs nothing.
        if test.databases:
            note_automation()
        self.manifests = manifests
        self.machines: tuple[Machine, ...] = (Machine(APP), Machine(DB), Machine(EDGE))
        self.reaches: frozenset[tuple[str, str]] | None = None
        for patcher in (
            patch("hq.platform.application.automation_adoption.generation", return_value="supplied"),
            patch(
                "hq.platform.application.automation_adoption.manifests", side_effect=lambda _generation: self.manifests
            ),
            # Supplied without the adoption that titles them, so none has a title.
            patch("hq.platform.application.automation_adoption.playbook_titles", side_effect=lambda *_: {}),
            patch("hq.platform.application.automation_runs.machine_catalog", side_effect=lambda: self.machines),
            patch("hq.platform.application.automation_runs.runner_reaches", side_effect=self._reaches),
        ):
            patcher.start()
            test.addCleanup(patcher.stop)
        test.addCleanup(clear_plugin_composition_cache)
        clear_plugin_composition_cache()

    def __call__(self, *manifests: OperationManifest) -> None:
        self.manifests = manifests
        clear_plugin_composition_cache()

    def know(self, *machines: Machine) -> None:
        self.machines = machines

    def reach(self, *reaches: tuple[str, str]) -> None:
        """The worker's logins, each a machine and a profile, and no others."""

        self.reaches = frozenset(reaches)

    def _reaches(self) -> frozenset[tuple[str, str]]:
        from ..automation_runs import login_profile

        if self.reaches is not None:
            return self.reaches
        profiles = {login_profile(manifest) for manifest in self.manifests}
        return frozenset((machine.name, profile) for machine in self.machines for profile in profiles)


def run_payload(*hosts: str, service: str = "web", **fields: Any) -> dict[str, Any]:
    return {"hosts": list(hosts or (APP,)), "parameters": {"service": service}, **fields}


APPROVER = "example-owner"


def approved_run(
    test: TestCase, payload: dict[str, Any], *, principal: Any, key: str, name: str = RESTART_RUN
) -> dict[str, Any]:
    """The run an agent asked for, once a person approved it: the only way an
    agent's change reaches the automation worker. Answers what the run's
    command answered."""

    from django.contrib.auth import get_user_model

    from ..approvals import ApproveCommand, approve
    from ..capabilities import run_capability
    from ..security import web_principal

    held = run_capability(name, payload, principal=principal, idempotency_key=key).answer
    test.assertEqual(held.get("status"), "awaiting_approval", held)
    person, _ = get_user_model().objects.get_or_create(username=APPROVER)
    return approve(ApproveCommand(held["approval"]["id"]), principal=web_principal(person))["result"]
