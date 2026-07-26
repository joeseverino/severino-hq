"""Resolve permission-safe read models from the trusted topology snapshot."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from django.db import transaction

from core.audit import operation_context

from .models import ManagedResource, TopologySnapshot
from .providers import ProviderResolutionContext, resolve_provider_spec, validate_spec


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

        declared_keys: set[str] = set()
        for declaration in validated["managed_resources"]:
            key = declaration["key"]
            declared_keys.add(key)
            resource = (
                ManagedResource.objects.select_for_update().filter(key=key).first()
            )
            if (
                resource
                and resource.declaration_source
                != ManagedResource.DeclarationSource.TOPOLOGY
            ):
                raise TopologyError(
                    f"Topology cannot take ownership of manual resource {key!r}."
                )
            desired_enabled = declaration.get("enabled", True)
            desired_fingerprint = _desired_fingerprint(validated, declaration)
            if resource is None:
                resource = ManagedResource(
                    key=key,
                    kind=declaration["kind"],
                    spec=declaration["spec"],
                    enabled=desired_enabled,
                    desired_fingerprint=desired_fingerprint,
                    declaration_source=ManagedResource.DeclarationSource.TOPOLOGY,
                )
                resource.full_clean()
                resource.save()
                continue
            changed = (
                resource.kind != declaration["kind"]
                or resource.spec != declaration["spec"]
                or resource.enabled != desired_enabled
                or resource.desired_fingerprint != desired_fingerprint
            )
            if not changed:
                continue
            resource.kind = declaration["kind"]
            resource.spec = declaration["spec"]
            resource.enabled = desired_enabled
            resource.desired_fingerprint = desired_fingerprint
            resource.generation += 1
            resource.full_clean()
            resource.save()

        stale_resources = (
            ManagedResource.objects.select_for_update()
            .filter(
                declaration_source=ManagedResource.DeclarationSource.TOPOLOGY,
                enabled=True,
            )
            .exclude(key__in=declared_keys)
        )
        for resource in stale_resources:
            resource.enabled = False
            resource.generation += 1
            resource.full_clean()
            resource.save()
    return snapshot


def _desired_fingerprint(
    payload: dict[str, Any], declaration: dict[str, Any]
) -> str:
    """Fingerprint the complete desired input, including resolved references."""
    desired: dict[str, Any] = {
        "kind": declaration["kind"],
        "spec": declaration["spec"],
        "enabled": declaration.get("enabled", True),
    }
    provider = resolve_provider_spec(
        declaration["kind"],
        declaration["spec"],
        context=ProviderResolutionContext(topology=payload),
    )
    if provider != declaration["spec"]:
        desired["resolved"] = provider
    return hashlib.sha256(_canonical(desired)).hexdigest()


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
