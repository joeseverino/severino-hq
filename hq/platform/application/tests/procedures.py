"""Synthetic ``example.*`` procedures and machines, supplied as an extension and HQ's facts would be."""

from typing import Annotated, Any
from unittest.mock import patch

from django.test import TestCase
from pydantic import BaseModel, ConfigDict, Field

from ..automation_manifest import OperationManifest
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
    run resolves its hosts through.
    """

    def __init__(self, test: TestCase, *manifests: OperationManifest) -> None:
        self.manifests = manifests
        self.machines: tuple[Machine, ...] = (Machine(APP), Machine(DB), Machine(EDGE))
        for patcher in (
            patch("hq.platform.application.plugins.plugin_operations", side_effect=lambda: self.manifests),
            patch("hq.platform.application.automation_runs.machine_catalog", side_effect=lambda: self.machines),
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


def run_payload(*hosts: str, service: str = "web", **fields: Any) -> dict[str, Any]:
    return {"hosts": list(hosts or (APP,)), "parameters": {"service": service}, **fields}
