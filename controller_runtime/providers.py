"""The controller's dispatch: which handler serves each kind, action and connection.

Every handler registers itself in ``handlers`` beside its own definition, so the
tables here are derived rather than listed. ``REGISTRANTS`` is the closed set of
modules admitted to register one; importing them is admitting them.
"""

from __future__ import annotations

from collections.abc import Callable
import os
import logging
from typing import Any

from control_plane.providers import (
    PROVIDERS,
    controller_capability_registry,
)
from control_plane.connection_kinds import CONNECTION_CREDENTIALS
from control_plane.provider_adapters.contracts import (
    CREDENTIAL_REFUSAL,
    ProviderError,
    ProviderResult,
    failure_of,
)
from control_plane.provider_adapters import onepassword
from control_plane.provider_adapters.tailscale import TAILNET_KIND
from control_plane.provider_adapters.parts import part_ledger
from . import cloudflare, commands, connection_env, handlers, host_readings, portainer, provider_http, provider_runtime, tailscale, tls
from .handlers import probes

logger = logging.getLogger("severino.controller")

REGISTRANTS = (tls, cloudflare, portainer, tailscale, host_readings, provider_runtime)


@probes("onepassword")
def _probe_onepassword(connection_ref: str) -> dict[str, Any]:
    return onepassword.probe(provider_runtime._RUNTIME, connection_ref)


def _probe_ssh(connection_ref: str) -> dict[str, Any]:
    commands._ssh(connection_ref, "preflight")
    transport = connection_env._transport(connection_ref)
    return {
        "detail": f"{transport['user']}@{transport['host']}:{transport['port']}",
        "reaches": [transport["host"]],
    }


PROVIDER_INVENTORY = {**handlers.INVENTORY, **handlers.OBSERVATION_READERS}

_CONNECTION_PROBES = handlers.PROBES

_DEFAULT_CONNECTION_ENDPOINTS = {"tailscale": tailscale.TAILNET_API}


def _endpoint(prefix: str, provider: str) -> str:
    """Where a connection points, from whichever values its projection produced.

    Never a secret: a URL and a host are what an operator needs to recognise
    which of two connections they are looking at, and both are already visible
    to anyone who can reach the thing at all.
    """

    for name in ("URL", "DIRECTORY_URL"):
        url = os.environ.get(f"{prefix}_{name}", "").strip()
        if url:
            return url
    host = os.environ.get(f"{prefix}_HOST", "").strip()
    port = os.environ.get(f"{prefix}_PORT", "").strip()
    if host:
        return f"{host}:{port}" if port else host
    return _DEFAULT_CONNECTION_ENDPOINTS.get(provider, "")


def connections(*, carry: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """See _connections. Opens the per-sweep snapshot when the caller has not."""

    if provider_http._PROVIDER_SNAPSHOT.get() is not None:
        return _connections(carry=carry)
    with provider_http.provider_snapshot():
        return _connections(carry=carry)


def _connections(*, carry: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """Every connection the environment carries, and whether it answers.

    One failure is that connection's failure. Reported rather than raised so a
    Cloudflare token that expired does not also make the two machines HQ can
    still reach look like they have gone away: the sweep is the only thing
    that tells an operator which of the two happened.

    `carry` is HQ's list of SSH connections whose last answer is recent and
    good. Those are reported as carried rather than probed: a probe of an SSH
    connection is a real login, and the active sweep cadence would make that one
    a minute. Still reported, because a connection left out of a sweep is one HQ
    removes.
    """

    ssh_refs = set(connection_env.ssh_connection_refs())
    reported: list[dict[str, Any]] = []
    for connection_ref, prefix in sorted(connection_env.connection_prefixes().items()):
        provider = connection_env.effective_provider(connection_ref, ssh_refs)
        probe = _CONNECTION_PROBES.get(provider)
        if probe is None and connection_ref in ssh_refs:
            probe = _probe_ssh
        connection = {
            "connection_ref": connection_ref,
            "provider": provider,
            "endpoint": _endpoint(prefix, provider),
            "manages": connection_env.connection_manages(connection_ref),
            "probed": probe is not None,
            "ok": True,
            "detail": "",
            "reaches": [],
        }
        if store := connection_env.connection_store(prefix):
            connection["store"] = store
        if probe is _probe_ssh and connection_ref in carry:
            # Recorded as unprobed if HQ has no earlier answer to keep.
            connection.update(
                carried=True, probed=False, detail="Not asked again this sweep."
            )
        elif probe is None:
            # Carried, usable, and not something this knows how to ask. Reported
            # as unprobed rather than omitted: a connection HQ cannot see is one
            # an operator will keep re-adding.
            connection["detail"] = "No probe for this kind of connection."
        else:
            connection.update(_probed(probe, connection_ref))
        reported.append(connection)
    return reported


def _probed(probe: Callable[[str], dict[str, Any]], connection_ref: str) -> dict[str, Any]:
    """What one probe found: its detail and reach, and the expiry it read."""

    try:
        result = probe(connection_ref)
        found = {"detail": result["detail"], "reaches": result["reaches"]}
    except (ProviderError, OSError, ValueError, KeyError) as exc:
        return {"ok": False, "detail": str(exc), "failure": failure_of(exc)}
    if result.get("expires_at"):
        found["expires_at"] = result["expires_at"]
    return found


def inventory(*, only: frozenset[str] = frozenset()) -> dict[str, Any]:
    """See _inventory. Opens the per-sweep snapshot when the caller has not."""

    if provider_http._PROVIDER_SNAPSHOT.get() is not None:
        return _inventory(only)
    with provider_http.provider_snapshot():
        return _inventory(only)


def _inventory(only: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Everything each provider holds, whether or not HQ declared it; with
    ``only``, just those kinds. The reconcilers fetch these lists in full
    anyway, so reporting the rest costs the provider nothing extra.

    One unreachable provider reports as unreachable rather than failing the
    sweep. Losing the whole inventory because a single service is restarting
    would make the least reliable provider decide whether HQ can see any of them.
    """

    found: dict[str, Any] = {}
    ssh_refs = set(connection_env.ssh_connection_refs())
    connected = {connection_env.effective_provider(ref, ssh_refs) for ref in connection_env.connection_prefixes()}
    for kind, lister in PROVIDER_INVENTORY.items():
        if only and kind not in only:
            continue
        if not _has_source(kind, connected):
            found[kind] = {"ok": True, "records": [], "connected": False}
            continue
        found[kind] = _read_kind(lister)
    return found


def _read_kind(lister: Callable[[], list[dict[str, Any]]]) -> dict[str, Any]:
    """One kind's report: its records and the parts refused while they were read,
    or the refusal of the whole read."""

    with part_ledger() as refused:
        try:
            report: dict[str, Any] = {"ok": True, "records": lister()}
        except (ProviderError, OSError, ValueError, KeyError) as exc:
            return _refused_report(exc)
    if refused:
        report["refused_parts"] = refused
    return report


def _refused_report(exc: BaseException) -> dict[str, Any]:
    """A failed read, with whether the credential or one permission was refused."""

    refusal = getattr(exc, "refusal", "")
    report: dict[str, Any] = {"ok": False, "records": [], "error": str(exc)}
    if refusal:
        report["refusal"] = refusal
    reason = getattr(exc, "reason", "")
    if refusal == CREDENTIAL_REFUSAL and reason:
        report["error"] = reason
    return report


# Kinds read from something mounted on this host, and whether it is mounted.
_LOCAL_SOURCES: dict[str, Callable[[], bool]] = {
    TAILNET_KIND: lambda: bool(tailscale.TAILNET_STATUS),
    "host.firewall": lambda: bool(host_readings.HOST_FIREWALL),
}


def _has_source(kind: str, connected: set[str]) -> bool:
    """Whether anything this controller holds can read the kind.

    A kind whose provider is not a connection provider is read only from its
    local source; with none mounted it is not connected.
    """

    from control_plane.observations import OBSERVATIONS

    if kind in OBSERVATIONS:
        needs = (OBSERVATIONS[kind].provider,)
    elif kind in PROVIDERS:
        needs = tuple(PROVIDERS[kind].connection_providers)
    else:
        return True
    needs = tuple(provider for provider in needs if provider in CONNECTION_CREDENTIALS)
    if set(needs) & connected:
        return True
    local = _LOCAL_SOURCES.get(kind)
    return bool(local and local())


def _refuses(reason: str):
    """A handler for an action the registry says this controller will not take.

    The reason is the registry's, not a second copy of it here. A locked action
    that reached a controller is a bug in whatever queued it, and the operator
    reading the failure should be told the same thing the declaration form told
    them.
    """

    def locked(
        spec: dict[str, Any],
        *,
        apply: bool,
        observed: dict[str, Any] | None = None,
    ) -> ProviderResult:
        del spec, apply, observed
        raise ProviderError(reason)

    return locked


# Locked actions are generated, so a provider declared as locked needs no entry
# here at all, and cannot be declared locked while quietly having a handler
# that acts.
PROVIDER_ACTIONS = {
    **{
        (kind, action): _refuses(policy.reason)
        for kind, capability in controller_capability_registry().capabilities.items()
        for action, policy in capability.actions.items()
        if policy.mode == "locked"
    },
    **handlers.ACTIONS,
}


def _refuse_unless_managed(kind: str, spec: dict[str, Any]) -> None:
    """Refuse a write through a connection whose item does not declare manages.

    The same rule HQ applies before it offers the action: a resource naming its
    connection needs that one to manage; one naming none needs every
    connection of its kind's providers to manage, and at least one to exist.
    """

    provider = PROVIDERS.get(kind)
    if provider is None or not provider.connection_providers:
        return
    named = str(spec.get("connection_ref") or "")
    if named:
        refs: tuple[str, ...] = (named,)
    else:
        ssh_refs = set(connection_env.ssh_connection_refs())
        refs = tuple(
            ref
            for ref in sorted(connection_env.connection_prefixes())
            if connection_env.effective_provider(ref, ssh_refs) in provider.connection_providers
        )
    if not refs:
        raise ProviderError(
            "No connection this controller holds manages this. Set manages on "
            "the connection to act through it."
        )
    observing = [ref for ref in refs if not connection_env.connection_manages(ref)]
    if observing:
        raise ProviderError(
            f"{', '.join(observing)} only observes. Set manages on the "
            "connection to act through it."
        )


def execute(
    resource: dict[str, Any], action: str, *, apply: bool = True
) -> ProviderResult:
    identity = (resource["kind"], action)
    try:
        handler = PROVIDER_ACTIONS[identity]
    except KeyError as exc:
        raise ProviderError(
            f"Unsupported provider/action: {identity[0]}/{identity[1]}."
        ) from exc
    capability = controller_capability_registry().capabilities.get(identity[0])
    policy = capability.actions.get(action) if capability else None
    if apply and (policy is None or policy.mode != "locked"):
        _refuse_unless_managed(identity[0], resource["spec"])
    return handler(
        resource["spec"], apply=apply, observed=resource.get("observed") or {}
    )
