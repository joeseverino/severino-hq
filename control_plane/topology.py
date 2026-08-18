"""The topology describes what exists. HQ decides what should be configured.

These were one thing until HQ took ownership of desired state. The topology
document declared managed resources, and every import re-materialised them --
which meant a rewrite edited in HQ was silently reverted by the next sync, and
the edit looked like it had worked right up until it did not.

So the document now describes the world: hosts, containers, certificates, and
the dependencies between them. It no longer declares what should be true of
them. A managed resource is authored in HQ and nowhere else.

One tie remains, and it is deliberate. A certificate's authored spec is a single
topology reference, so the consumers behind that reference decide what the
controller actually has to do while the authored spec stays byte-identical.
Importing therefore re-fingerprints resolved desired state and advances the
generation of anything whose resolution moved -- without that, a dependency
change would leave the resource looking in sync forever.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from django.db import transaction

from core.audit import operation_context

from .models import ManagedResource, TopologySnapshot
from .providers import ProviderResolutionContext, resolve_provider_spec


class TopologyError(ValueError):
    pass


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def validate_topology(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TopologyError("Topology must be a JSON object.")
    if payload.get("version") != 3:
        raise TopologyError("HQ requires topology schema version 3.")
    for field in ("hosts", "pki", "externals", "dependencies"):
        if not isinstance(payload.get(field), list):
            raise TopologyError(f"Topology field {field!r} must be a list.")

    refs = {
        *(f"host:{host['id']}" for host in payload["hosts"]),
        *(f"pki:{entry['id']}" for entry in payload["pki"]),
        *(f"external:{entry['id']}" for entry in payload["externals"]),
    }
    for host in payload["hosts"]:
        refs.update(
            f"container:{host['id']}/{container['id']}"
            for container in host.get("containers", [])
        )
    for dependency in payload["dependencies"]:
        for endpoint in ("from", "to"):
            if dependency.get(endpoint) not in refs:
                raise TopologyError(
                    f"Dependency has dangling {endpoint} reference "
                    f"{dependency.get(endpoint)!r}."
                )

    # A legacy 'managed_resources' block is tolerated here and refused on import.
    # This function also validates snapshots already stored, which were written
    # while the document still declared them -- rejecting those would break
    # certificate resolution for every resource on the strength of a block
    # nothing reads any more.
    return payload


def _refuse_declared_resources(payload: dict[str, Any]) -> None:
    """Refused rather than ignored, at the point of authoring.

    A block quietly dropped on import would leave the operator believing the
    document still governs these -- which is the belief that made an HQ edit
    look like it worked and then vanish on the next sync.
    """

    declared = payload.get("managed_resources") or []
    if not declared:
        return
    keys = ", ".join(
        sorted(
            str(entry.get("key", "?")) for entry in declared if isinstance(entry, dict)
        )
    )
    raise TopologyError(
        "HQ owns managed resources; the topology document no longer declares "
        f"them. Remove the 'managed_resources' block ({keys}) -- HQ already holds "
        "these, and importing them would overwrite edits made in HQ."
    )


def desired_fingerprint(
    kind: str,
    spec: dict[str, Any],
    enabled: bool,
    *,
    topology: dict[str, Any] | None = None,
) -> str:
    """Fingerprint the complete desired input, including resolved references.

    The authored spec alone is not the desired state. A certificate declares one
    topology reference and nothing else; everything the controller must actually
    do is on the far side of resolving it. Two callers need the same answer --
    an HQ edit, and a topology import that moved what a reference resolves to --
    so there is one function rather than one each.

    An unresolvable reference fingerprints the authored spec instead of raising.
    HQ can hold a resource pointing at a certificate the topology has not
    described yet; that is a thing to show on the resource, not a reason to
    fail an import of unrelated hosts.
    """

    desired: dict[str, Any] = {"kind": kind, "spec": spec, "enabled": enabled}
    try:
        resolved = resolve_provider_spec(
            kind, spec, context=ProviderResolutionContext(topology=topology)
        )
    except (KeyError, TypeError, ValueError):
        resolved = spec
    if resolved != spec:
        desired["resolved"] = resolved
    return hashlib.sha256(_canonical(desired)).hexdigest()


@transaction.atomic
def import_topology(payload: object) -> TopologySnapshot:
    validated = validate_topology(payload)
    _refuse_declared_resources(validated)
    canonical = _canonical(validated)
    checksum = hashlib.sha256(canonical).hexdigest()
    with operation_context(
        interface="sync",
        actor="topology-sync",
        operation="infrastructure.topology.import",
    ):
        snapshot = (
            TopologySnapshot.objects.select_for_update()
            .filter(id="topology")
            .first()
        )
        if snapshot is None:
            snapshot = TopologySnapshot.objects.create(
                id="topology",
                schema_version=validated["version"],
                checksum=checksum,
                payload=validated,
            )
        elif (
            snapshot.schema_version != validated["version"]
            or snapshot.checksum != checksum
            or snapshot.payload != validated
        ):
            snapshot.schema_version = validated["version"]
            snapshot.checksum = checksum
            snapshot.payload = validated
            snapshot.save()

        _advance_resolved_state(validated)
    return snapshot


def _advance_resolved_state(payload: dict[str, Any]) -> None:
    """Mark for reconciliation anything the new topology silently changed.

    The resource was not edited -- its authored spec is identical -- but what
    that spec resolves to is not. Advancing the generation is what tells the
    controller there is work, and what stops the resource reporting itself in
    sync against a world that moved underneath it.
    """

    for resource in ManagedResource.objects.select_for_update().filter(enabled=True):
        fingerprint = desired_fingerprint(
            resource.kind, resource.spec, resource.enabled, topology=payload
        )
        if fingerprint == resource.desired_fingerprint:
            continue
        # A resource HQ has never fingerprinted is being adopted into the scheme,
        # not changed by it. Advancing its generation would queue a reconcile for
        # every existing resource the first time this ran.
        adopting = not resource.desired_fingerprint
        resource.desired_fingerprint = fingerprint
        if not adopting:
            resource.generation += 1
        resource.full_clean()
        resource.save()


def resolve_certificate(topology_ref: str) -> dict[str, Any]:
    try:
        payload = TopologySnapshot.objects.get(pk="topology").payload
    except TopologySnapshot.DoesNotExist as exc:
        raise TopologyError("No trusted topology snapshot has been imported.") from exc
    validate_topology(payload)
    return resolve_provider_spec(
        "tls.certificate",
        {"topology_ref": topology_ref},
        context=ProviderResolutionContext(topology=payload),
    )
