"""Resolve permission-safe read models from the trusted topology snapshot."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from django.db import transaction

from .models import ManagedResource, TopologySnapshot
from .providers import validate_spec


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
    for field in ("hosts", "pki", "externals", "dependencies", "managed_resources"):
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
    resource_keys: set[str] = set()
    for declaration in payload["managed_resources"]:
        if not isinstance(declaration, dict):
            raise TopologyError("Managed resource declarations must be objects.")
        try:
            key = declaration["key"]
            kind = declaration["kind"]
            spec = declaration["spec"]
        except KeyError as exc:
            raise TopologyError(
                f"Managed resource is missing field {exc.args[0]!r}."
            ) from exc
        if not isinstance(key, str) or not key:
            raise TopologyError("Managed resource keys must be non-empty strings.")
        if key in resource_keys:
            raise TopologyError(f"Duplicate managed resource key {key!r}.")
        resource_keys.add(key)
        try:
            declaration["spec"] = validate_spec(kind, spec)
        except (KeyError, TypeError, ValueError) as exc:
            raise TopologyError(
                f"Managed resource {key!r} is invalid: {exc}"
            ) from exc
    return payload


@transaction.atomic
def import_topology(payload: object) -> TopologySnapshot:
    validated = validate_topology(payload)
    canonical = _canonical(validated)
    snapshot, _ = TopologySnapshot.objects.update_or_create(
        id="topology",
        defaults={
            "schema_version": validated["version"],
            "checksum": hashlib.sha256(canonical).hexdigest(),
            "payload": validated,
        },
    )
    declared_keys: set[str] = set()
    for declaration in validated["managed_resources"]:
        key = declaration["key"]
        declared_keys.add(key)
        resource = ManagedResource.objects.select_for_update().filter(key=key).first()
        if resource and resource.declaration_source != ManagedResource.DeclarationSource.TOPOLOGY:
            raise TopologyError(
                f"Topology cannot take ownership of manual resource {key!r}."
            )
        if resource is None:
            ManagedResource.objects.create(
                key=key,
                kind=declaration["kind"],
                spec=declaration["spec"],
                enabled=declaration.get("enabled", True),
                declaration_source=ManagedResource.DeclarationSource.TOPOLOGY,
            )
            continue
        changed = (
            resource.kind != declaration["kind"]
            or resource.spec != declaration["spec"]
            or resource.enabled != declaration.get("enabled", True)
        )
        resource.kind = declaration["kind"]
        resource.spec = declaration["spec"]
        resource.enabled = declaration.get("enabled", True)
        if changed:
            resource.generation += 1
        resource.full_clean()
        resource.save()
    ManagedResource.objects.filter(
        declaration_source=ManagedResource.DeclarationSource.TOPOLOGY
    ).exclude(key__in=declared_keys).update(enabled=False)
    return snapshot


def resolve_certificate(topology_ref: str) -> dict[str, Any]:
    try:
        payload = TopologySnapshot.objects.get(pk="topology").payload
    except TopologySnapshot.DoesNotExist as exc:
        raise TopologyError("No trusted topology snapshot has been imported.") from exc
    validate_topology(payload)
    if not topology_ref.startswith("pki:"):
        raise TopologyError("Certificate topology references must start with 'pki:'.")
    certificate_id = topology_ref.removeprefix("pki:")
    certificate = next(
        (entry for entry in payload["pki"] if entry["id"] == certificate_id),
        None,
    )
    if certificate is None:
        raise TopologyError(f"Topology certificate {topology_ref!r} was not found.")
    consumers = [
        {
            "topology_ref": dependency["from"],
            **dependency.get("attributes", {}),
        }
        for dependency in payload["dependencies"]
        if dependency.get("relation") == "consumes"
        and dependency.get("to") == topology_ref
    ]
    if not consumers:
        raise TopologyError(f"Topology certificate {topology_ref!r} has no consumers.")
    return {
        "certificate_name": certificate.get("certificate_name", certificate_id),
        "domains": certificate.get("domains", []),
        "consumers": consumers,
    }
