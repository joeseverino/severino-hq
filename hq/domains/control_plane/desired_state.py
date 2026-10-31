"""What a resource is supposed to be, including whatever it resolves to.

A certificate names where it installs, and how each place takes a certificate
is stated on the place, so the fingerprint covers the resolved form and a
resource can fall out of date without anyone editing it. Why a save is then
not local: docs/Architecture.md#infrastructure-control-plane.
"""

import hashlib
import json
from collections.abc import Callable
from typing import Any

lazy from hq.platform.application.infrastructure import _NamesByConnection

from .models import ManagedResource
from .provider_spec import ProviderResolutionContext
from .providers import resolve_provider_spec


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def desired_fingerprint(
    kind: str,
    spec: dict[str, Any],
    *,
    enabled: bool,
    targets: tuple[dict[str, Any], ...] = (),
    resource_key: str = "",
    names_at: Callable[[str], tuple[str, ...]] | None = None,
) -> str:
    """Fingerprint the complete desired input, including resolved references.

    An unresolvable reference fingerprints the authored spec instead of raising:
    a certificate naming a target that does not exist yet is a thing to show on
    the resource, not a reason to refuse the save that would fix it.
    """

    if names_at is None:
        # The real derivation, never nothing: the contract handed to a
        # controller is resolved separately by a caller that supplies its own,
        # and resolved differently (a forgotten argument at any call site) the
        # fingerprint never matches what it fingerprints, so every pass sees a
        # change, advances the generation and queues the work again, silently
        # and forever. An explicit argument is the override. Built per call:
        # as the signature's default it would be evaluated at import, inside
        # the cycle with application.infrastructure, which imports this module.
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
    except KeyError, TypeError, ValueError:
        resolved = spec
    if resolved != spec:
        desired["resolved"] = resolved
    return hashlib.sha256(_canonical(desired)).hexdigest()


def advance_dependents(targets: tuple[dict[str, Any], ...]) -> list[str]:
    """Queue work for anything whose resolution moved, and say what moved.

    A resource HQ has never fingerprinted is being adopted into the scheme, not
    changed by it. Advancing its generation would queue a reconcile for every
    existing resource.
    """

    advanced = []
    for resource in ManagedResource.objects.select_for_update().filter(enabled=True):
        fingerprint = desired_fingerprint(
            resource.kind,
            resource.spec,
            enabled=resource.enabled,
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
