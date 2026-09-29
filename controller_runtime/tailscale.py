"""The tailnet: devices, routes, and what the tailnet reports.

The access policy is ``tailnet_policy``'s; the API client is ``tailnet_api``'s.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from control_plane.provider_adapters.contracts import (
    PERMISSION_REFUSAL,
    ProviderError,
    ProviderResult,
)
from control_plane.observations.tailscale import EXIT_ROUTES, SETTING_PARTS as TAILNET_SETTING_PARTS
from control_plane.provider_adapters.parts import refuse_part
from .handlers import acts, lists, probes, reads
from control_plane.provider_adapters.tailscale import TAILNET_KIND
from . import provider_http, tailnet_api, tailnet_policy


TAILNET_STATUS = os.environ.get("SEVERINO_TAILNET_STATUS", "")


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
            identities = _tailnet_identities(tailnet_api.tailnet_token(""))
        except ProviderError:
            identities = {}
    else:
        try:
            token = tailnet_api.tailnet_token("")
        except ProviderError as exc:
            raise ProviderError(
                "This controller was not given a tailnet reading or a tailnet "
                f"credential, so it cannot say which machines are up. {exc}"
            ) from None
        listed = tailnet_api.tailnet_api_devices(token)
        devices = [record for record in map(_api_device_record, listed) if record]
        identities = _identities_from(listed)
    # Who may reach each one, where a credential makes that answerable. Folded
    # into the device rather than swept separately: it is a fact about that
    # device, and a second inventory kind would be a second thing to join.
    reach = tailnet_policy.reach_by_device(devices)
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
        # machine is currently routing through: a statement about the reading
        # machine's own preference. `ExitNodeOption` is whether the peer offers to be one at
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
                provider_http.condition("Ready", True, "Reconciled", "The device is as declared.")
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
    token = tailnet_api.tailnet_token(spec["connection_ref"])
    try:
        with provider_http.open_url(
            f"{tailnet_api.TAILNET_API}/device/{urllib.parse.quote(identifier)}/key",
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
        provider_http.release(exc)
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
            provider_http.condition("Ready", True, "Reconciled", "The device is as declared.")
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
        with provider_http.open_url(
            f"{tailnet_api.TAILNET_API}/device/{urllib.parse.quote(identifier)}/routes",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        ) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        provider_http.release(exc)
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
        with provider_http.open_url(
            f"{tailnet_api.TAILNET_API}/device/{urllib.parse.quote(identifier)}/routes",
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
        provider_http.release(exc)
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
    token = tailnet_api.tailnet_token(spec.get("connection_ref", ""))
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
                provider_http.condition(
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
            provider_http.condition(
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


# An exit node is advertised as the two default routes rather than as a flag,
# so "does this offer to be an exit node" is a question about its route list.


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
        devices = tailnet_api.tailnet_api_devices(token)
    except ProviderError:
        return {}
    return _identities_from(devices)


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
            "offers_exit_node": bool(EXIT_ROUTES & set(advertised)),
            "exit_node_approved": bool(EXIT_ROUTES & set(enabled)),
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

    found = tailnet_api.tailnet_read("dns/configuration", "DNS configuration", "dns:read")
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

    found = tailnet_api.tailnet_read("settings", "settings", "feature_settings:read")
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

    found = tailnet_api.tailnet_read("users", "users", "users:read")
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


@probes("tailscale")
def _probe_tailscale(connection_ref: str) -> dict[str, Any]:
    """Prove the OAuth client is accepted without retaining its access token."""

    tailnet_api.tailnet_token(connection_ref)
    return {"detail": "OAuth credential accepted.", "reaches": []}
