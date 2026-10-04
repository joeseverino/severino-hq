"""Continuous delivery: every extension's latest admission runs in production.

The declaration. The controller (controller/providers/github_delivery.go)
compares the running image's lock with each extension's latest admission on
GitHub and reports each stage there; HQ starts nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field

from ..observations.github import PROVIDER
from ..provider_spec import ConnectionKind, ProviderModel, ProviderSpec, applies

KIND = "github.delivery"
CURRENT = "Every extension's latest admission, confirmed on GitHub"
COMPOSE_WORKFLOW = ".github/workflows/compose.yml"


def _from_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "repository": record["repository"],
        "workflow": record["workflow"],
        "branch": record["branch"],
        "production": record["production"],
    }


def _readout(spec: dict[str, Any], status: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    rows = [("Production", spec.get("production", ""), status.get("production", ""))]
    rows.extend(
        (item.get("plugin", ""), item.get("admitted", "")[:7], item.get("running", "")[:7])
        for item in status.get("extensions") or ()
        if isinstance(item, Mapping)
    )
    return tuple(rows)


class GitHubDeliverySpec(ProviderModel):
    repository: str = Field(
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
        title="Host repository",
        description="The repository whose composition workflow deploys HQ.",
    )
    workflow: str = Field(
        default=COMPOSE_WORKFLOW,
        pattern=r"^\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml$",
        title="Composition workflow",
    )
    branch: str = Field(default="main", pattern=r"^[A-Za-z0-9_./-]+$", title="Branch")
    production: Literal[CURRENT] = Field(  # type: ignore[valid-type]
        default=CURRENT, title="Production runs"
    )

DEFINITION = ProviderSpec(
    KIND,
    "Reports each extension's stage on its commit, from admission to "
    "production, and comments once on its merged pull request.",
    GitHubDeliverySpec,
    actions={"reconcile": applies(automatic=True)},
    label="Continuous delivery",
    connection_providers=(PROVIDER,),
    from_record=_from_record,
    identity=lambda spec: (spec["repository"],),
    key_hint=lambda record: "delivery",
    adopts=lambda record: record.get("production") == CURRENT,
    sample_record={
        "repository": "example/host",
        "workflow": COMPOSE_WORKFLOW,
        "branch": "main",
        "production": CURRENT,
        "extensions": [],
    },
    readout=_readout,
    advanced_fields=("workflow", "branch", "production"),
    declaration_only=True,
    removal_note=lambda spec: (
        "HQ stops reporting on extension commits. Their admissions still "
        f"start the composition in {spec.get('repository', 'the host repository')}."
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
