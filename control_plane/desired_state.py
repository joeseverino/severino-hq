"""What a resource is supposed to be, including whatever it resolves to.

The authored spec alone is not desired state. A certificate names where it
installs; how each of those places takes a certificate is stated on the place.
So two resources are involved in one answer, and a resource can fall out of date
without anyone editing it -- which is why the fingerprint covers the resolved
form and not just the authored one.

That is also why a save is not always local. Editing a target changes what every
certificate installed there resolves to, and a certificate left holding its old
fingerprint would report itself in sync against a world that moved underneath
it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from .models import ManagedResource
from .providers import ProviderResolutionContext, resolve_provider_spec


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def desired_fingerprint(
    kind: str,
    spec: dict[str, Any],
    enabled: bool,
    *,
    targets: tuple[dict[str, Any], ...] = (),
    resource_key: str = "",
    names_at: Callable[[str], tuple[str, ...]] | None = None,
) -> str:
    """Fingerprint the complete desired input, including resolved references.

    An unresolvable reference fingerprints the authored spec instead of raising.
    HQ can hold a certificate naming a target that does not exist yet; that is a
    thing to show on the resource, not a reason to refuse the save that would
    fix it.

    ``names_at`` defaults to the real derivation rather than to nothing, and
    that choice is load-bearing. This fingerprint decides whether desired state
    has moved, and the contract handed to a controller is resolved separately by
    a caller that supplies its own. Resolve those two differently -- which is
    what a forgotten argument at any of three call sites would do -- and the
    fingerprint never matches the thing it is fingerprinting: every pass sees a
    change, advances the generation, and queues the work again. Silently, and
    forever.

    So the safe value is the real one, and an explicit argument is the override
    rather than the requirement.
    """

    if names_at is None:
        # Deferred on purpose: application.infrastructure imports this module,
        # so at module scope this is a cycle. Inside the call it is resolved
        # after both modules exist, which is the whole reason for the default
        # being constructed here rather than in the signature.
        from application.infrastructure import _NamesByConnection

        names_at = _NamesByConnection()
    desired: dict[str, Any] = {"kind": kind, "spec": spec, "enabled": enabled}
    try:
        resolved = resolve_provider_spec(
            kind,
            spec,
            context=ProviderResolutionContext(
                delivery_targets=targets,
                resource_key=resource_key,
                names_at=names_at,
            ),
        )
    except (KeyError, TypeError, ValueError):
        resolved = spec
    if resolved != spec:
        desired["resolved"] = resolved
    return hashlib.sha256(_canonical(desired)).hexdigest()


def advance_dependents(targets: tuple[dict[str, Any], ...]) -> list[str]:
    """Queue work for anything whose resolution moved, and say what moved.

    A resource HQ has never fingerprinted is being adopted into the scheme, not
    changed by it. Advancing its generation would queue a reconcile for every
    existing resource the first time this ran.
    """

    advanced = []
    for resource in ManagedResource.objects.select_for_update().filter(enabled=True):
        fingerprint = desired_fingerprint(
            resource.kind,
            resource.spec,
            resource.enabled,
            targets=targets,
            resource_key=resource.key,
        )
        if fingerprint == resource.desired_fingerprint:
            continue
        adopting = not resource.desired_fingerprint
        resource.desired_fingerprint = fingerprint
        if not adopting:
            resource.generation += 1
            advanced.append(resource.key)
        resource.full_clean()
        resource.save()
    return advanced
