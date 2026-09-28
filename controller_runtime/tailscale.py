"""The tailnet: devices, routes, the access policy, and what the tailnet reports."""

from __future__ import annotations

import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from control_plane.provider_adapters.contracts import (
    CREDENTIAL_REFUSAL,
    PERMISSION_REFUSAL,
    ProviderError,
    ProviderResult,
)
from control_plane.observations.tailscale import SETTING_PARTS as TAILNET_SETTING_PARTS
from control_plane.provider_adapters.parts import refuse_part
from controller_runtime.tailnet_policy import refuse_weaker_tests
from .handlers import acts, lists, probes, reads
from control_plane.provider_adapters.tailscale import TAILNET_KIND, TAILNET_POLICY_KIND
from . import connection_env, portainer, provider_http


TAILNET_STATUS = os.environ.get("SEVERINO_TAILNET_STATUS", "")
# Tailnet lock, handed over the same way and for the same reason: the local
# API is read *and* write, so the controller is given a reading rather than the
# socket. Separately optional: a tailnet without lock enabled answers it
# perfectly well, and a daemon too old to know it should cost the sweep
# nothing.
TAILNET_LOCK = os.environ.get("SEVERINO_TAILNET_LOCK", "")


def _tailnet_lock() -> dict[str, Any]:
    """Whether tailnet lock is on, and who it is currently shutting out.

    The fact with the least warning attached. Under lock a node whose key no
    signing node has signed is not degraded, it is *absent*: every other node
    filters it out, and the node itself reports being perfectly healthy. There
    is nothing in a status page or a service check that says why.
    """

    if not TAILNET_LOCK:
        return {}
    try:
        status = json.loads(Path(TAILNET_LOCK).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(status, dict):
        return {}
    return {
        "enabled": bool(status.get("Enabled")),
        # Whether the machine taking this reading is itself signed. A "no" here
        # is why the rest of the tailnet cannot see it.
        "node_key_signed": bool(status.get("NodeKeySigned")),
        "trusted_keys": len(status.get("TrustedKeys") or ()),
        # Named, not counted: a locked-out node is a machine somebody has to go
        # and sign, and a number does not say which.
        "locked_out": sorted(
            str(peer.get("Name") or peer.get("StableID") or "")
            for peer in status.get("FilteredPeers") or ()
        ),
    }


def local_tailnet_devices() -> list[dict[str, Any]]:
    """The tailnet as the local daemon sees it, and nothing more.

    Kept apart from the enriched sweep because the two have different costs and
    different reasons. This one needs no credential and no network call beyond
    a unix socket, so anything that only wants to know what a device *is*
    (reconciling one, for instance) asks this rather than paying for a policy
    read it will not use.
    """

    if not TAILNET_STATUS:
        raise ProviderError(
            "This controller was not given a tailnet reading, so it cannot say "
            "which machines are up."
        )
    try:
        raw = Path(TAILNET_STATUS).read_text(encoding="utf-8")
    except OSError as exc:
        raise ProviderError(
            "The tailnet reading is missing. It is taken from the local "
            "daemon before this container starts, and only when there is one."
        ) from exc
    try:
        status = json.loads(raw)
    except ValueError as exc:
        raise ProviderError("The tailnet reading is not readable status.") from exc
    nodes = [status.get("Self") or {}, *(status.get("Peer") or {}).values()]
    found = [record for record in map(_tailnet_record, nodes) if record]
    # Which of them took the reading. Every other device is described from its
    # point of view (the relay carrying it, the bytes exchanged with it) so
    # a reader that cannot tell which one is the observer is reading a set of
    # measurements with no origin.
    for record in found[:1]:
        record["self"] = True
    return found


@lists(TAILNET_KIND)
def list_tailnet_devices() -> list[dict[str, Any]]:
    """Every machine on the tailnet, with what the policy says about each.

    Read from the daemon this machine is already a peer of rather than from
    Tailscale's API. There is no credential to hold, render or rotate: a node's
    view of its own tailnet is something it has by being on it, and a controller
    that cannot be given a token cannot leak one.

    Handed in as a file rather than fetched here. The daemon's local API is read
    *and* write, with no read-only mode, so a controller holding its socket
    could log this machine off the tailnet, and this is the process that holds
    every provider credential. It only ever needed the reading, so the reading
    is what it gets.

    It answers the question no other sweep can. Every other provider reports
    whether a *service* answered, so a machine whose Portainer token expired and
    a machine that is switched off are indistinguishable. This tells them apart.

    The local view is deliberately not the whole picture: tags, the policy
    file and the tailnet's DNS configuration are control-plane facts the daemon
    does not hold. What it does hold is presence and key expiry, which are the
    two that go wrong quietly.
    """

    # The local daemon's reading when one is mounted, since only it knows paths
    # and relays. Otherwise the coordination server's list, which a tailnet
    # credential alone can read.
    if TAILNET_STATUS:
        devices = local_tailnet_devices()
        try:
            identities = _tailnet_identities(_tailnet_token(""))
        except ProviderError:
            identities = {}
    else:
        try:
            token = _tailnet_token("")
        except ProviderError as exc:
            raise ProviderError(
                "This controller was not given a tailnet reading or a tailnet "
                f"credential, so it cannot say which machines are up. {exc}"
            ) from None
        listed = _tailnet_api_devices(token)
        devices = [record for record in map(_api_device_record, listed) if record]
        identities = _identities_from(listed)
    # Who may reach each one, where a credential makes that answerable. Folded
    # into the device rather than swept separately: it is a fact about that
    # device, and a second inventory kind would be a second thing to join.
    reach = _reach_by_device(devices)
    for device in devices:
        device["reach"] = reach.get(device["name"], [])
        identity = identities.get(device["name"], {})
        device["user"] = identity.get("user", "")
        device["tags"] = identity.get("tags", [])
        device["advertised_routes"] = identity.get("advertised_routes", [])
        device["enabled_routes"] = identity.get("enabled_routes", [])
        device["authorized"] = bool(identity.get("authorized", True))
        device["lock_error"] = identity.get("lock_error", "")
        device["update_available"] = bool(identity.get("update_available"))
        device["client_version"] = identity.get("client_version", "")
        device["ssh_enabled"] = bool(identity.get("ssh_enabled"))
        device["blocks_incoming"] = bool(identity.get("blocks_incoming"))
        device["external"] = bool(identity.get("external"))
        # The coordination server's answer wins over the daemon's inference:
        # the daemon reports no expiry date, which is the same shape whether
        # expiry is disabled or the reading simply lacks it.
        if "key_expiry_disabled" in identity:
            device["key_expiry_disabled"] = bool(identity["key_expiry_disabled"])
        # The daemon's `ExitNodeOption` answers "can this node be my exit node",
        # which is already false for one that offers but was never approved.
        # The coordination server is the only side that can tell those apart,
        # so when it answered, its answer wins.
        if device["name"] in identities:
            device["offers_exit_node"] = bool(identity.get("offers_exit_node"))
            device["exit_node_approved"] = bool(identity.get("exit_node_approved"))
        else:
            device["exit_node_approved"] = device.get("offers_exit_node", False)
    return devices


def _tailnet_record(node: dict[str, Any]) -> dict[str, Any] | None:
    """One machine, keyed by the name the rest of HQ already calls it.

    Every field is optional. Tailscale omits rather than nulls (a device with
    expiry disabled has no ``KeyExpiry`` at all, and a machine that has never
    been seen has no ``LastSeen``) so a reader that requires any of them
    rejects exactly the devices it exists to describe.
    """

    name = str(node.get("HostName") or "").strip()
    if not name:
        return None
    return {
        "name": name,
        # The node's WireGuard public key. The cryptographic identity itself:
        # a peering is not a claim in an inventory, it is two keys that have
        # completed a handshake, and this is the half that can be shown.
        "public_key": str(node.get("PublicKey") or ""),
        # The MagicDNS name, which is how the tailnet addresses it and not
        # always what the host calls itself.
        "dns_name": str(node.get("DNSName") or "").rstrip("."),
        "online": bool(node.get("Online")),
        "last_seen": str(node.get("LastSeen") or ""),
        # Absent means expiry is disabled for this device, which is a different
        # statement from "expires at some unknown time" and is kept distinct.
        "key_expires": str(node.get("KeyExpiry") or ""),
        "addresses": [str(address) for address in node.get("TailscaleIPs") or ()],
        "os": str(node.get("OS") or ""),
        # Two different questions, and only the second is a fact about the
        # device. `ExitNode` is whether this peer is the exit node *this*
        # machine is currently routing through: a statement about our own
        # preference. `ExitNodeOption` is whether the peer offers to be one at
        # all. A machine page saying "exit node" means the latter.
        "exit_node_in_use": bool(node.get("ExitNode")),
        "offers_exit_node": bool(node.get("ExitNodeOption")),
        "self": False,
        # How the traffic actually gets there, which is the part nothing else
        # can answer. A peer is either reached directly (the two daemons found
        # a path through both NATs) or carried by a relay, and the difference
        # is a real one an operator otherwise has to shell in to see. Absent
        # means the peer is idle rather than unreachable: a path is negotiated
        # when there is traffic, so a machine nobody is talking to has neither.
        "direct_endpoint": str(node.get("CurAddr") or ""),
        # Every address this node can be reached at off the tailnet: the one
        # its router hands out and the one the internet sees it as. Reported
        # only for the node taking the reading; a peer's own list is not
        # something the daemon is told.
        "endpoints": [str(endpoint) for endpoint in node.get("Addrs") or ()],
        "relay": str(node.get("Relay") or ""),
        "last_handshake": str(node.get("LastHandshake") or ""),
        "active": bool(node.get("Active")),
        "rx_bytes": int(node.get("RxBytes") or 0),
        "tx_bytes": int(node.get("TxBytes") or 0),
    }


def _api_device_record(device: dict[str, Any]) -> dict[str, Any] | None:
    """One device from the coordination server, in the local reading's shape.

    Path fields (direct endpoint, relay, handshake, traffic) are the daemon's
    to know and stay empty.
    """

    name = str(device.get("hostname") or "").strip()
    if not name:
        return None
    connectivity = device.get("clientConnectivity") or {}
    return {
        "name": name,
        "public_key": str(device.get("nodeKey") or ""),
        "dns_name": str(device.get("name") or "").rstrip("."),
        "online": bool(device.get("connectedToControl")),
        "last_seen": str(device.get("lastSeen") or ""),
        "key_expires": "" if device.get("keyExpiryDisabled") else str(device.get("expires") or ""),
        "addresses": [str(address) for address in device.get("addresses") or ()],
        "os": str(device.get("os") or ""),
        "exit_node_in_use": False,
        "offers_exit_node": False,
        "self": False,
        "direct_endpoint": "",
        "endpoints": [str(endpoint) for endpoint in connectivity.get("endpoints") or ()],
        "relay": "",
        "last_handshake": "",
        "active": False,
        "rx_bytes": 0,
        "tx_bytes": 0,
    }


TAILNET_API = "https://api.tailscale.com/api/v2"


def _tailnet_token(connection_ref: str) -> str:
    """Exchange once per sweep and retain no token beyond that snapshot.

    The token lasts an hour, but a process-wide cache adds expiry and revocation
    behavior to get wrong. One sweep needs it several times, so that sweep shares
    one exchange and drops the result when its snapshot closes. The client itself
    remains held by the vault rather than by this process.
    """

    prefix = connection_env.connection_prefix("tailscale", connection_ref)

    def exchange() -> str:
        client_id = provider_http._required(prefix, "CLIENT_ID")
        client_secret = provider_http._required(prefix, "CLIENT_SECRET")
        body = urllib.parse.urlencode(
            {"client_id": client_id, "client_secret": client_secret}
        ).encode()
        try:
            with provider_http._open(
                f"{TAILNET_API}/oauth/token",
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
                timeout=30,
            ) as response:
                payload = json.loads(response.read())
                if not isinstance(payload, dict):
                    raise ValueError("OAuth response is not an object")
                token = payload.get("access_token", "")
        except urllib.error.HTTPError as exc:
            provider_http._release(exc)
            reason = (
                f"Tailscale refused the credential for {connection_ref} "
                f"({exc.code}). It has to be an OAuth client, not an API key."
            )
            raise ProviderError(
                reason, refusal=CREDENTIAL_REFUSAL, reason=reason
            ) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ProviderError("Tailscale did not answer the token request.") from exc
        if not token:
            raise ProviderError("Tailscale returned no access token.")
        return token

    return provider_http._snapshot_value(("tailscale-token", prefix), exchange)


def _tailnet_device_id(name: str) -> str:
    """The device's stable id, taken from the local daemon rather than the API.

    The reading this controller already has carries it, so finding which device
    to change costs no call and no credential. The token is spent on the change
    itself and on nothing else.
    """

    for node in _tailnet_nodes():
        if str(node.get("HostName") or "").strip() == name:
            identifier = str(node.get("ID") or "")
            if identifier:
                return identifier
    raise ProviderError(
        f"No device called {name!r} is on the tailnet this machine can see."
    )


@acts(TAILNET_KIND, "reconcile")
def reconcile_tailnet_device(
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    """Assert HQ's decision about one device, and report what is true after.

    Only the settings HQ declares are touched. Everything else about the device
    its name, its tags, its routes, whether it is even switched on: belongs
    to the machine and to whoever runs it.
    """

    del observed
    name = spec["name"]
    wanted = bool(spec.get("key_expiry_disabled"))
    current = _tailnet_device_state(name)
    if current["key_expiry_disabled"] == wanted:
        return ProviderResult(
            changed=False,
            status=current,
            conditions=[
                provider_http._condition("Ready", True, "Reconciled", "The device is as declared.")
            ],
            message="Tailnet device is current.",
        )
    if not apply:
        return ProviderResult(
            changed=True,
            status=current,
            conditions=[],
            message=(
                f"Key expiry would be {'disabled' if wanted else 'enabled'} for {name}."
            ),
        )

    identifier = _tailnet_device_id(name)
    token = _tailnet_token(spec["connection_ref"])
    try:
        with provider_http._open(
            f"{TAILNET_API}/device/{urllib.parse.quote(identifier)}/key",
            data=json.dumps({"keyExpiryDisabled": wanted}).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
            timeout=30,
        ) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        provider_http._release(exc)
        if exc.code == 403:
            raise ProviderError(
                "This Tailscale credential may not change devices. It needs "
                "the devices:core scope."
            ) from exc
        raise ProviderError(
            f"Tailscale refused the change to {name} ({exc.code})."
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ProviderError("Tailscale did not answer the change request.") from exc

    return ProviderResult(
        changed=True,
        status={**current, "key_expiry_disabled": wanted, "key_expires": ""},
        conditions=[
            provider_http._condition("Ready", True, "Reconciled", "The device is as declared.")
        ],
        message=(
            f"{name} now stays on the tailnet."
            if wanted
            else f"{name} has an expiry date again."
        ),
    )


# Each names the scope its own call needs. devices:routes includes the read.
_TAILNET_ROUTES_READ_SCOPE = (
    "This Tailscale credential may not read routes. It needs the "
    "devices:routes:read scope, or devices:routes to approve them."
)
_TAILNET_ROUTES_WRITE_SCOPE = (
    "This Tailscale credential may not approve routes. It needs the "
    "devices:routes scope."
)


def _tailnet_routes(name: str, identifier: str, token: str) -> dict[str, Any]:
    """The routes one device advertises and has enabled, as Tailscale holds them."""

    try:
        with provider_http._open(
            f"{TAILNET_API}/device/{urllib.parse.quote(identifier)}/routes",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        ) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        provider_http._release(exc)
        # Named at the first call: an operator told only that the routes could
        # not be read goes looking at the device.
        if exc.code in (401, 403):
            raise ProviderError(_TAILNET_ROUTES_READ_SCOPE) from exc
        raise ProviderError(f"Tailscale did not report the routes for {name}.") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ProviderError(f"Tailscale did not report the routes for {name}.") from exc


def _enable_tailnet_routes(
    name: str, identifier: str, token: str, routes: list[str]
) -> dict[str, Any]:
    """Set the device's enabled routes to exactly ``routes``; Tailscale's answer."""

    try:
        with provider_http._open(
            f"{TAILNET_API}/device/{urllib.parse.quote(identifier)}/routes",
            data=json.dumps({"routes": routes}).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
            timeout=30,
        ) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        provider_http._release(exc)
        if exc.code in (401, 403):
            raise ProviderError(_TAILNET_ROUTES_WRITE_SCOPE) from exc
        raise ProviderError(
            f"Tailscale refused the route approval for {name}."
        ) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ProviderError(f"Tailscale did not answer for {name}.") from exc


@acts(TAILNET_KIND, "approve-routes")
def approve_tailnet_routes(
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    """Approve exactly the routes this device is already advertising.

    Approval takes the whole set, so it is read before it is written: sending a
    list assembled from anywhere else would silently withdraw a route this call
    was never about. What the machine offers is what gets approved, and a
    machine offering nothing is a no-op rather than a way to clear its routes.
    """

    del observed
    name = spec["name"]
    identifier = _tailnet_device_id(name)
    token = _tailnet_token(spec.get("connection_ref", ""))
    current = _tailnet_routes(name, identifier, token)
    advertised = sorted(str(route) for route in current.get("advertisedRoutes") or ())
    enabled = sorted(str(route) for route in current.get("enabledRoutes") or ())
    pending = [route for route in advertised if route not in set(enabled)]
    status = {
        "name": name,
        "advertised_routes": advertised,
        "enabled_routes": enabled,
    }
    if not pending:
        return ProviderResult(
            changed=False,
            status=status,
            conditions=[
                provider_http._condition(
                    "Ready",
                    True,
                    "Reconciled",
                    "Every route this device advertises is approved.",
                )
            ],
            message=(
                "Nothing to approve." if advertised else f"{name} advertises no routes."
            ),
        )
    if not apply:
        return ProviderResult(
            changed=True,
            status=status,
            conditions=[],
            message=f"Would approve {', '.join(pending)} for {name}.",
        )

    approved = _enable_tailnet_routes(name, identifier, token, advertised)
    status["enabled_routes"] = sorted(
        str(route) for route in approved.get("enabledRoutes") or ()
    )
    return ProviderResult(
        changed=True,
        status=status,
        conditions=[
            provider_http._condition(
                "Ready", True, "Reconciled", "The advertised routes are approved."
            )
        ],
        message=f"Approved {', '.join(pending)} for {name}.",
    )


def _tailnet_device_state(name: str) -> dict[str, Any]:
    """What the tailnet currently says about one device."""

    for record in local_tailnet_devices():
        if record["name"] == name:
            return {
                "name": name,
                "online": record["online"],
                "key_expires": record["key_expires"],
                # No expiry is the setting, not an unknown date.
                "key_expiry_disabled": not record["key_expires"],
            }
    raise ProviderError(
        f"No device called {name!r} is on the tailnet this machine can see."
    )


def _tailnet_nodes() -> list[dict[str, Any]]:
    """The raw local reading, for the fields the record does not carry."""

    if not TAILNET_STATUS:
        raise ProviderError("This controller was not given a tailnet reading.")
    try:
        status = json.loads(Path(TAILNET_STATUS).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProviderError("The tailnet reading is missing or unreadable.") from exc
    return [status.get("Self") or {}, *(status.get("Peer") or {}).values()]


def _tailnet_get(token: str, path: str) -> dict[str, Any]:
    """One tailnet-level read. Raises ProviderError with the reason."""

    try:
        with provider_http._open(
            f"{TAILNET_API}/tailnet/-/{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        ) as response:
            found = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        provider_http._release(exc)
        raise ProviderError(f"/{path} answered HTTP {exc.code}.") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        provider_http._release(exc)
        raise ProviderError(f"/{path} could not be read: {type(exc).__name__}.") from None
    if not isinstance(found, dict):
        raise ProviderError(f"/{path} did not answer with an object.")
    return found


def _tailnet_parts(token: str, parts: dict[str, tuple[str, ...]]) -> dict:
    """Several tailnet reads for one record, each a declared part: what was
    read. A part refused is reported through ``refuse_part``."""

    read: dict[str, dict[str, Any]] = {}
    for name, paths in parts.items():
        merged: dict[str, Any] = {}
        for path in paths:
            try:
                merged.update(_tailnet_get(token, path))
            except ProviderError as exc:
                refuse_part(name, exc)
                break
        read[name] = merged
    return read


def _tailnet_policy_etag(token: str) -> str:
    """The version of the policy HQ read, so a write cannot clobber a newer one.

    Without it, two people editing at once means the later save silently wins.
    Tailscale takes this back as ``If-Match`` and refuses the write instead.
    """

    try:
        with provider_http._open(
            f"{TAILNET_API}/tailnet/-/acl",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=30,
        ) as response:
            return response.headers.get("etag", "")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        provider_http._release(exc)
        return ""


def _policy_passes_its_tests(token: str, document: dict[str, Any]) -> None:
    """The gate. Validation runs the tests the document carries, so a change
    that would break one is refused before anything is written."""

    try:
        with provider_http._open(
            f"{TAILNET_API}/tailnet/-/acl/validate",
            data=json.dumps(document).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
            timeout=30,
        ) as response:
            verdict = json.loads(response.read() or b"{}")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as exc:
        provider_http._release(exc)
        raise ProviderError("Tailscale could not check the policy.") from exc
    if verdict:
        raise ProviderError(
            "The declared policy does not pass its own tests, so it was not "
            f"applied: {json.dumps(verdict)[:300]}"
        )


def _write_tailnet_policy(token: str, document: dict[str, Any]) -> None:
    """Write the policy, conditional on the version last read."""

    etag = _tailnet_policy_etag(token)
    try:
        with provider_http._open(
            f"{TAILNET_API}/tailnet/-/acl",
            data=json.dumps(document).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                **({"If-Match": etag} if etag else {}),
            },
            method="POST",
            timeout=30,
        ) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        provider_http._release(exc)
        if exc.code == 412:
            raise ProviderError(
                "The policy changed somewhere else since HQ read it, so this "
                "was not applied. Read it again and make the change on top."
            ) from exc
        raise ProviderError(f"Tailscale refused the policy ({exc.code}).") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ProviderError("Tailscale did not answer the policy write.") from exc


def _current_policy(document: dict[str, Any]) -> ProviderResult:
    """The live policy already is the declared one; Ready only if it is tested."""

    tested = bool(document.get("tests"))
    return ProviderResult(
        changed=False,
        status={"applied": True},
        conditions=[
            provider_http._condition("Ready", True, "Reconciled", "The policy is as declared.")
            if tested
            else provider_http._condition(
                "Ready",
                False,
                "Untested",
                "The policy is as declared and carries no tests, so nothing "
                "checks what it grants.",
            )
        ],
        message="Tailnet policy is current."
        if tested
        else "Tailnet policy is current and untested.",
    )


@acts(TAILNET_POLICY_KIND, "reconcile")
def reconcile_tailnet_policy(
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    """Apply the declared policy, but only if it still passes its own tests.

    The policy is the tailnet's security boundary, and its failure mode is
    locking everybody out of everything at once. Tailscale will validate a
    document on request and run the tests written inside it, so that is the
    gate: a policy whose tests fail is refused here rather than applied and
    regretted. The console warns about that; this declines.

    Conditional on the version last read, so a change made somewhere else in
    the meantime stops this rather than being overwritten by it.
    """

    del observed
    wanted = (spec.get("document") or "").strip()
    if not wanted:
        return ProviderResult(
            changed=False,
            status={},
            conditions=[],
            message="No policy is declared, so there is nothing to apply.",
        )
    try:
        document = json.loads(wanted)
    except ValueError as exc:
        raise ProviderError("The declared policy is not readable JSON.") from exc
    token = _tailnet_token(spec.get("connection_ref", ""))
    live = _tailnet_policy(token)
    if live == document:
        return _current_policy(document)
    refuse_weaker_tests(live, document)
    _policy_passes_its_tests(token, document)
    if not apply:
        return ProviderResult(
            changed=True,
            status={},
            conditions=[],
            message="The policy passes its own tests and would be applied.",
        )
    _write_tailnet_policy(token, document)
    return ProviderResult(
        changed=True,
        status={"applied": True},
        conditions=[
            provider_http._condition("Ready", True, "Reconciled", "The policy is as declared.")
        ],
        message="Tailnet policy applied after its own tests passed.",
    )


def _tailnet_policy(token: str) -> dict[str, Any]:
    """The tailnet's policy file, as Tailscale currently holds it."""

    try:
        with provider_http._open(
            f"{TAILNET_API}/tailnet/-/acl",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=30,
        ) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        provider_http._release(exc)
        raise _tailnet_refused("the policy read", "policy_file:read", exc.code) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ProviderError("Tailscale did not return a readable policy.") from exc


def _who_may_reach(
    policy: dict[str, Any], token: str, target: str
) -> list[dict[str, Any]]:
    """The rules that let anything reach one address and port.

    Asked of Tailscale rather than worked out here. HQ is a reader of this
    policy and must not become a second implementation of it: an answer derived
    locally would be believed exactly as much as the real one and wrong in ways
    nobody would notice until it mattered.
    """

    try:
        with provider_http._open(
            f"{TAILNET_API}/tailnet/-/acl/preview"
            f"?type=ipport&previewFor={urllib.parse.quote(target)}",
            data=json.dumps(policy).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
            timeout=30,
        ) as response:
            return json.loads(response.read()).get("matches") or []
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as exc:
        provider_http._release(exc)
        # One address that cannot be previewed must not lose the others.
        return []


# The attribute an app connector is declared under. Named once: it appears in
# the policy as a key, and a second spelling of it would read as a second
# feature rather than as a typo.
_APP_CONNECTOR_ATTR = "tailscale.com/app-connectors"


def _app_connectors(policy: dict[str, Any]) -> list[dict[str, Any]]:
    """Every app connector the policy declares, as its own fact.

    An app connector is a node routing traffic for named domains on the
    tailnet's behalf, so it is a way something is reached that is neither a
    device nor a DNS record, and it is declared inside the policy rather than
    anywhere HQ was looking.
    """

    found = []
    for attr in policy.get("nodeAttrs") or []:
        for declared in (attr.get("app") or {}).get(_APP_CONNECTOR_ATTR) or []:
            found.append(
                {
                    "name": str(declared.get("name", "")),
                    "connectors": sorted(
                        str(node) for node in declared.get("connectors") or ()
                    ),
                    "domains": sorted(
                        str(domain) for domain in declared.get("domains") or ()
                    ),
                }
            )
    return found


@lists(TAILNET_POLICY_KIND)
def list_tailnet_policy() -> list[dict[str, Any]]:
    """The policy itself: who is grouped, what is tagged, and what it grants.

    Read so HQ can show the thing its verdicts come from. A reachability answer
    an operator cannot trace to a rule is one they have to take on faith, and
    the rules are small enough to put on a page.
    """

    # Not caught here. The sweep records a raising collector as unreachable,
    # keeps what the kind last held and carries the reason. Every failure on
    # this path (no credential rendered, a client that is not an OAuth
    # client, a refused read) raises with its own message. Swallowed into an
    # empty list they all became the same thing: a successful sweep of a
    # tailnet with no policy, so nothing was unreachable and nothing said why.
    token = _tailnet_token("")
    policy = _tailnet_policy(token)
    parts = _tailnet_parts(
        token,
        {
            "settings": ("settings",),
            "dns": ("dns/preferences", "dns/nameservers", "dns/searchpaths"),
            "services": ("services",),
        },
    )
    return [
        {
            "record": "policy",
            # The document itself, so a declaration can hold it and be compared
            # against reality without a second read.
            "document": json.dumps(policy, indent=2, sort_keys=True),
            # The aliases the policy gives addresses. A grant may name a device
            # by one, and then the alias is the name that admits it, as real
            # a principal as a user or a tag, and the only one HQ could not see
            # from a device reading alone.
            "hosts": {
                str(name): str(address)
                for name, address in (policy.get("hosts") or {}).items()
            },
            "settings": parts["settings"],
            "dns": parts["dns"],
            "groups": [
                {"name": name, "members": sorted(members)}
                for name, members in sorted((policy.get("groups") or {}).items())
            ],
            "tags": [
                {"name": name, "owners": sorted(owners)}
                for name, owners in sorted((policy.get("tagOwners") or {}).items())
            ],
            "grants": [
                {
                    "src": sorted(grant.get("src") or []),
                    "dst": sorted(grant.get("dst") or []),
                    "ip": sorted(grant.get("ip") or []),
                }
                for grant in policy.get("grants") or []
            ],
            "tests": policy.get("tests") or [],
            "lock": _tailnet_lock(),
            # A Service is a name the tailnet serves that is not a device,
            # published by whichever nodes advertise it, and reachable under
            # the policy like anything else. Nothing else in HQ would notice
            # one appearing. Read from /services, which needs services:read.
            "services": [
                {
                    "name": str(service.get("name", "")),
                    "addresses": sorted(str(a) for a in service.get("addrs") or ()),
                    "comment": str(service.get("comment", "")),
                    "ports": sorted(str(p) for p in service.get("ports") or ()),
                }
                for service in parts["services"].get("vipServices") or []
            ],
            # Not fetched: both are declared inside the policy this record
            # already carries, so reading them is reading it.
            "app_connectors": _app_connectors(policy),
            # Tailscale SSH rules are grants like any other (who may open a
            # shell, on what, as which user) and the grants table above shows
            # none of them because they live under their own key.
            "ssh_rules": [
                {
                    "action": str(rule.get("action", "")),
                    "src": sorted(str(s) for s in rule.get("src") or ()),
                    "dst": sorted(str(d) for d in rule.get("dst") or ()),
                    "users": sorted(str(u) for u in rule.get("users") or ()),
                }
                for rule in policy.get("ssh") or []
            ],
        }
    ]


def _reach_by_device(devices: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Who the policy lets reach each device, on the ports worth asking about.

    Swept rather than asked live, because the web process holds no credential
    and is not going to start: "who can reach this" is answered from what a
    controller already went and got, the same as every other reading on the
    page it appears on.

    Silent when there is no Tailscale credential. The devices themselves come
    from the local daemon and need none, so a controller without one still
    reports presence and simply cannot say who may reach it.
    """

    try:
        token = _tailnet_token("")
        policy = _tailnet_policy(token)
    except ProviderError:
        return {}

    # Groups are flattened here, where the policy is. HQ then answers "may this
    # device reach that one" by asking whether its identity is in a list,
    # which is reading Tailscale's answer, not re-deriving it. Expanding groups
    # in HQ would be the first step toward a second policy engine.
    members = {
        group: set(users) for group, users in (policy.get("groups") or {}).items()
    }

    def flatten(names: list[str]) -> list[str]:
        out: set[str] = set()
        for name in names:
            out.update(members.get(name, {name}))
        return sorted(out)

    asking = _ports_worth_asking()
    found: dict[str, list[dict[str, Any]]] = {}
    for device in devices:
        # IPv4 only. The policy here is written against v4, and previewing both
        # families would double every row to say the same thing twice.
        address = next((a for a in device["addresses"] if ":" not in a), "")
        if not address:
            continue
        for port in asking:
            matches = _who_may_reach(policy, token, f"{address}:{port}")
            raw = sorted(
                {name for match in matches for name in (match.get("users") or [])}
            )
            if raw:
                found.setdefault(device["name"], []).append(
                    {
                        "port": port,
                        "who": flatten(raw),
                        # The rule itself, so a verdict can show what decided it
                        # rather than only what it decided. Line numbers are the
                        # policy's own, which is how an operator finds it.
                        "rules": [
                            {
                                "who": sorted(match.get("users") or []),
                                "to": sorted(match.get("ports") or []),
                                "line": match.get("lineNumber"),
                            }
                            for match in matches
                        ],
                    }
                )
    return found


# An exit node is advertised as the two default routes rather than as a flag,
# so "does this offer to be an exit node" is a question about its route list.
_EXIT_ROUTES = frozenset({"0.0.0.0/0", "::/0"})


def _tailnet_identities(token: str) -> dict[str, dict[str, Any]]:
    """Everything the coordination server knows about each device, in one read.

    ``fields=all`` rather than a call per device: the default projection omits
    routes, and this keeps the sweep's cost independent of how many machines
    exist.

    From the API rather than the local daemon for two reasons. The daemon does
    not report tags, which is what a policy names a device by. And a peer's
    routes as the daemon sees them are the routes the ACL lets *this* node
    receive, so a route can be advertised, approved, and still absent from the
    local reading, which makes the daemon unable to tell "never offered" from
    "offered and refused", the one distinction worth reporting.
    """

    try:
        devices = _tailnet_api_devices(token)
    except ProviderError:
        return {}
    return _identities_from(devices)


def _tailnet_api_devices(token: str) -> list[dict[str, Any]]:
    """Every device as the coordination server lists it. Raises when refused."""

    try:
        with provider_http._open(
            f"{TAILNET_API}/tailnet/-/devices?fields=all",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        ) as response:
            found = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        provider_http._release(exc)
        raise _tailnet_refused(
            "the tailnet device list", "devices:core:read", exc.code
        ) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        provider_http._release(exc)
        raise ProviderError(f"The tailnet device list could not be read: {type(exc).__name__}.") from None
    if not isinstance(found, dict):
        raise ProviderError("The tailnet device list did not answer with an object.")
    return [device for device in found.get("devices") or () if isinstance(device, dict)]


def _identities_from(devices: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for device in devices:
        hostname = str(device.get("hostname", ""))
        if not hostname:
            continue
        advertised = sorted(str(r) for r in device.get("advertisedRoutes") or ())
        enabled = sorted(str(r) for r in device.get("enabledRoutes") or ())
        found[hostname] = {
            "user": str(device.get("user", "")),
            "tags": sorted(device.get("tags") or []),
            "advertised_routes": advertised,
            "enabled_routes": enabled,
            # Stated separately from the route lists because it is the question
            # an operator actually asks, and because the two default routes
            # being present is not obvious as an answer to it.
            "offers_exit_node": bool(_EXIT_ROUTES & set(advertised)),
            "exit_node_approved": bool(_EXIT_ROUTES & set(enabled)),
            # Facts with no symptom until they matter. A device the tailnet has
            # not authorised is on no network; one carrying a lock error cannot
            # be reached by anything under tailnet lock; and a client left
            # behind is how a fleet acquires versions nobody chose.
            "authorized": bool(device.get("authorized", True)),
            "lock_error": str(device.get("tailnetLockError") or ""),
            "update_available": bool(device.get("updateAvailable")),
            "client_version": str(device.get("clientVersion") or ""),
            # The coordination server's own answer, rather than the daemon's
            # absence-of-an-expiry inference.
            "key_expiry_disabled": bool(device.get("keyExpiryDisabled")),
            # Three more that travel in the same response and that nothing else
            # in HQ can answer. Tailscale SSH turns a device into something the
            # policy can hand shells out on; shields-up means it accepts no
            # inbound connection at all, which looks identical to being broken;
            # and an external device belongs to somebody else's tailnet and was
            # shared into this one.
            "ssh_enabled": bool(device.get("sshEnabled")),
            "blocks_incoming": bool(device.get("blocksIncomingConnections")),
            "external": bool(device.get("isExternal")),
        }
    return found


def _tailnet_refused(what: str, scope: str, status: int) -> ProviderError:
    """One refused tailnet read, classified.

    The token was just exchanged, so the client itself is valid: Tailscale
    answers 403, and 404 on some endpoints, to a client without the scope. A
    401 is the token refused.
    """

    if status in (403, 404):
        return ProviderError(
            f"Tailscale refused {what} ({status}). The credential needs the "
            f"{scope} scope.",
            refusal=PERMISSION_REFUSAL,
        )
    if status == 401:
        return ProviderError(
            f"Tailscale refused {what} ({status}).",
            refusal=CREDENTIAL_REFUSAL,
            reason=f"Tailscale refused the access token ({status}).",
        )
    return ProviderError(f"Tailscale refused {what} ({status}).")


def _tailnet_read(path: str, what: str, scope: str) -> dict[str, Any]:
    """One tailnet-level read. A refusal raises and names the scope it needs."""

    token = _tailnet_token("")
    try:
        with provider_http._open(
            f"{TAILNET_API}/tailnet/-/{path}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=30,
        ) as response:
            found = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        provider_http._release(exc)
        raise _tailnet_refused(f"the {what} read", scope, exc.code) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ProviderError(f"Tailscale did not return readable {what}.") from exc
    if not isinstance(found, dict):
        raise ProviderError(f"Tailscale did not return readable {what}.")
    return found


def _resolver_addresses(resolvers: Any) -> list[str]:
    """Resolver objects or bare addresses, as addresses."""

    found = []
    for resolver in resolvers or ():
        address = resolver.get("address") if isinstance(resolver, dict) else resolver
        if address:
            found.append(str(address))
    return found


@reads("tailscale.dns")
def list_tailnet_dns() -> list[dict[str, Any]]:
    """The tailnet's resolvers, MagicDNS, search paths and split DNS."""

    found = _tailnet_read("dns/configuration", "DNS configuration", "dns:read")
    preferences = found.get("preferences") or {}
    return [
        {
            "record": "dns",
            "nameservers": _resolver_addresses(found.get("nameservers")),
            "override_local_dns": bool(preferences.get("overrideLocalDNS")),
            "magic_dns": bool(preferences.get("magicDNS")),
            "search_paths": [str(path) for path in found.get("searchPaths") or ()],
            "split_dns": {
                str(domain): _resolver_addresses(resolvers)
                for domain, resolvers in (found.get("splitDNS") or {}).items()
            },
        }
    ]


@reads("tailscale.settings")
def list_tailnet_settings() -> list[dict[str, Any]]:
    """The tailnet-wide settings that decide who joins and how long keys last.
    A setting another scope governs and Tailscale withheld is its part refused."""

    found = _tailnet_read("settings", "settings", "feature_settings:read")
    for key, part in TAILNET_SETTING_PARTS.items():
        if found.get(key) is None:
            refuse_part(
                part.name,
                ProviderError(f"Tailscale withheld {key}.", refusal=PERMISSION_REFUSAL),
            )
    return [
        {
            "record": "settings",
            "devices_approval_on": found.get("devicesApprovalOn"),
            "devices_key_duration_days": found.get("devicesKeyDurationDays"),
            "devices_auto_updates_on": found.get("devicesAutoUpdatesOn"),
            "users_approval_on": found.get("usersApprovalOn"),
            "regional_routing_on": found.get("regionalRoutingOn"),
            "posture_identity_collection_on": found.get("postureIdentityCollectionOn"),
            "https_enabled": found.get("httpsEnabled"),
            "acls_externally_managed_on": found.get("aclsExternallyManagedOn"),
        }
    ]


@reads("tailscale.user")
def list_tailnet_users() -> list[dict[str, Any]]:
    """Who holds access to the tailnet, their role and when each was last seen."""

    found = _tailnet_read("users", "users", "users:read")
    return [
        {
            "id": str(user.get("id", "")),
            "display_name": str(user.get("displayName", "")),
            "login_name": str(user.get("loginName", "")),
            "role": str(user.get("role", "")),
            "status": str(user.get("status", "")),
            "created": str(user.get("created", "")),
            "last_seen": str(user.get("lastSeen", "")),
        }
        for user in found.get("users") or ()
        if isinstance(user, dict) and user.get("id")
    ]


# Where the answers start. Asking about all 65535 would be that many calls per
# device; these are the ports anything is reached on anywhere, and the rest are
# added from what this machine can actually see listening.
TAILNET_BASE_PORTS = (22, 53, 80, 443)


def _ports_worth_asking() -> tuple[int, ...]:
    """The ports something here actually listens on, plus the usual few.

    Derived rather than listed. A hardcoded set answers about ports nothing
    uses and says "cannot say" about the one an operator came to ask about,
    and the containers on this machine already state which ports they publish,
    so the set that matters is knowable rather than guessable.
    """

    found = set(TAILNET_BASE_PORTS)
    try:
        for container in portainer.list_portainer_containers():
            found.update(
                int(port)
                for port in container.get("ports") or ()
                if str(port).isdigit() and 0 < int(port) < 65536
            )
    except (ProviderError, OSError, ValueError, KeyError):
        # No Portainer, or it is not answering. The base set still applies, and
        # a sweep that reports fewer ports is better than one that reports none.
        pass
    return tuple(sorted(found))


@probes("tailscale")
def _probe_tailscale(connection_ref: str) -> dict[str, Any]:
    """Prove the OAuth client is accepted without retaining its access token."""

    _tailnet_token(connection_ref)
    return {"detail": "OAuth credential accepted.", "reaches": []}
