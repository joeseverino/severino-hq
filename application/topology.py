"""Topology synchronization orchestration shared by delivery adapters."""

from __future__ import annotations

from typing import Any

from django.db import models, transaction

from control_plane.models import ManagedResource
from control_plane.topology import import_topology

from .infrastructure import OperationCommand, request_reconcile
from .security import Principal


@transaction.atomic
def sync_topology(payload: object, *, principal: Principal) -> dict[str, Any]:
    """Materialize topology declarations and schedule pending generations."""
    snapshot = import_topology(payload)
    scheduled: list[dict[str, Any]] = []
    resources = ManagedResource.objects.filter(
        declaration_source=ManagedResource.DeclarationSource.TOPOLOGY,
        enabled=True,
    ).exclude(generation=models.F("observed_generation"))
    for resource in resources.order_by("key"):
        result = request_reconcile(
            OperationCommand(
                idempotency_key=(
                    f"topology:{snapshot.checksum}:{resource.key}:"
                    f"{resource.generation}:reconcile"
                ),
                reason="Desired state changed in the topology source of truth.",
            ),
            principal=principal,
            current_key=resource.key,
        )
        scheduled.append(
            {
                "resource": resource.key,
                "generation": resource.generation,
                "queued": result["queued"],
                "operation": result["operation"]["id"],
            }
        )
    return {
        "ok": True,
        "schema_version": snapshot.schema_version,
        "checksum": snapshot.checksum,
        "scheduled": scheduled,
    }
