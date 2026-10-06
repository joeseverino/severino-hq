"""Continuous delivery: every extension's latest admission runs in production.

The declaration. The controller (controller/providers/github_delivery.go)
compares the running image's lock with each extension's latest admission on
GitHub and reports each stage there; HQ starts nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field

from ..bridge_contract import keyword
from ..observations.github import PROVIDER
from ..provider_spec import ConnectionKind, ProviderModel, ProviderSpec, applies

KIND = "github.delivery"
# The bridge contract states these; the controller reports a sweep's record
# with the same defaults, which is what lets `adopts` recognise it.
_FIELDS = ("GitHubDeliverySpec", "properties")
CURRENT = keyword("GitHubDeliveryProduction", "enum", 0)
COMPOSE_WORKFLOW = keyword(*_FIELDS, "workflow", "default")
MAIN_BRANCH = keyword(*_FIELDS, "branch", "default")


def _from_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "repository": record["repository"],
        "workflow": record["workflow"],
        "branch": record["branch"],
        "production": record["production"],
    }


GOAL = "Production runs the latest approved commit of each plugin."


def _plugin_state(item: Mapping[str, Any]) -> str:
    """One plugin's line: what runs, and what stands between it and the latest."""

    running, approved = str(item.get("running", ""))[:7], str(item.get("admitted", ""))[:7]
    if not approved or approved == running:
        return f"Live at {running}" if running else "Live"
    stage = str(item.get("stage", "")).strip()
    return f"Runs {running}, latest approved is {approved}" + (f". {stage}." if stage else ".")


def _readout(spec: dict[str, Any], status: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    rows = [("Goal", GOAL, "")]
    rows.extend(
        (str(item.get("plugin", "")), "", _plugin_state(item))
        for item in status.get("extensions") or ()
        if isinstance(item, Mapping)
    )
    return tuple(rows)


def _console(record: dict[str, Any]) -> str:
    """The run a plugin is waiting on, else the repository's runs."""

    for item in record.get("extensions") or ():
        if isinstance(item, Mapping) and item.get("run_url"):
            return str(item["run_url"])
    repository = str(record.get("repository", ""))
    return f"https://github.com/{repository}/actions" if repository else ""


class GitHubDeliverySpec(ProviderModel):
    repository: str = Field(
        pattern=keyword(*_FIELDS, "repository", "pattern"),
        title="Repository",
        description="The repository whose deploy workflow deploys HQ.",
    )
    workflow: str = Field(
        default=COMPOSE_WORKFLOW,
        pattern=keyword(*_FIELDS, "workflow", "pattern"),
        title="Deploy workflow",
    )
    branch: str = Field(
        default=MAIN_BRANCH, pattern=keyword(*_FIELDS, "branch", "pattern"), title="Branch"
    )
    production: Literal[CURRENT] = Field(  # type: ignore[valid-type]
        default=CURRENT, title="Production"
    )

DEFINITION = ProviderSpec(
    KIND,
    "Checks that production runs the latest approved commit of each plugin, "
    "and reports it on the commit and its pull request.",
    GitHubDeliverySpec,
    actions={"reconcile": applies(automatic=True)},
    label="HQ deploys",
    label_plural="HQ deploys",
    name=lambda spec: "HQ deploys",
    reported_fields=("production",),
    console=_console,
    console_label="Open the deploy on GitHub",
    connection_providers=(PROVIDER,),
    from_record=_from_record,
    identity=lambda spec: (spec["repository"],),
    key_hint=lambda record: "delivery",
    adopts=lambda record: record.get("production") == CURRENT,
    sample_record={
        "repository": "example/host",
        "workflow": COMPOSE_WORKFLOW,
        "branch": MAIN_BRANCH,
        "production": CURRENT,
        "extensions": [],
    },
    readout=_readout,
    advanced_fields=("workflow", "branch", "production"),
    declaration_only=True,
    removal_note=lambda spec: (
        "HQ stops reporting on plugin commits. Their approvals still "
        f"start the deploy in {spec.get('repository', 'the repository')}."
    ),
)
DEFINITIONS = (DEFINITION,)

# The connection this provider's credential arrives through, beside its kinds:
# admitting the module admits both.
CONNECTIONS = {
    # An app's permissions are fine-grained and each token HQ mints is
    # narrowed again to one call's repositories and permissions.
    PROVIDER: ConnectionKind("GitHub App", "scoped"),
}
