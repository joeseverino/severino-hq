"""The tailnet policy: reading it, and writing a declared one safely.

A declared policy is written only after it clears its own tests and the live
policy's. Tailscale's validation runs only the tests a document carries, so it
is a gate only when those tests check at least what the live policy's tests
check. The same reading says who the policy lets reach each device.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from control_plane.provider_adapters.contracts import ProviderError, ProviderResult
from control_plane.provider_adapters.tailscale import TAILNET_POLICY_KIND
from . import portainer, provider_http, tailnet_api
from .handlers import acts, lists


def _policy_tests(policy: dict[str, Any]) -> dict[tuple[str, str], dict[str, set[str]]]:
    """A policy's tests keyed by (src, proto), with their accept and deny sets."""

    found: dict[tuple[str, str], dict[str, set[str]]] = {}
    for test in policy.get("tests") or ():
        if not isinstance(test, dict):
            continue
        pair = (str(test.get("src", "")), str(test.get("proto", "")))
        entry = found.setdefault(pair, {"accept": set(), "deny": set()})
        for verdict in ("accept", "deny"):
            entry[verdict].update(str(dst) for dst in test.get(verdict) or ())
    return found


def refuse_weaker_tests(live: dict[str, Any], document: dict[str, Any]) -> None:
    """Refuse a policy whose tests check less than the live policy's do.

    Tailscale's validation is the next gate, and it runs only the tests the new
    document carries. A document with no tests passes it unconditionally, so a
    gate that trusted it alone would wave through exactly the change it exists
    to stop. Every (src, proto) the live tests cover keeps a test, and every
    live deny stays a deny; accepts may be rewritten. A policy with no tests is
    not one HQ writes.
    """

    wanted = _policy_tests(document)
    held = _policy_tests(live)
    if not wanted:
        raise ProviderError(
            "The declared policy carries no tests, so Tailscale's check would "
            "pass it whatever it grants. It was not applied."
        )
    missing = sorted(pair for pair in held if pair not in wanted)
    if missing:
        src, proto = missing[0]
        raise ProviderError(
            f"The declared policy drops the tests for {src!r}"
            f"{f' over {proto}' if proto else ''}, which removes the check they "
            "made, so it was not applied."
        )
    for pair, entry in sorted(held.items()):
        dropped = sorted(entry["deny"] - wanted[pair]["deny"])
        if dropped:
            raise ProviderError(
                f"The declared policy no longer tests that {pair[0]!r} is denied "
                f"{dropped[0]!r}. A live deny is kept, so it was not applied."
            )


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


def _tailnet_policy_etag(token: str) -> str:
    """The version of the policy HQ read, so a write cannot clobber a newer one.

    Without it, two people editing at once means the later save silently wins.
    Tailscale takes this back as ``If-Match`` and refuses the write instead.
    """

    try:
        with provider_http.open_url(
            f"{tailnet_api.TAILNET_API}/tailnet/-/acl",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=30,
        ) as response:
            return response.headers.get("etag", "")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        provider_http.release(exc)
        return ""


def _policy_passes_its_tests(token: str, document: dict[str, Any]) -> None:
    """The gate. Validation runs the tests the document carries, so a change
    that would break one is refused before anything is written."""

    try:
        with provider_http.open_url(
            f"{tailnet_api.TAILNET_API}/tailnet/-/acl/validate",
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
        provider_http.release(exc)
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
        with provider_http.open_url(
            f"{tailnet_api.TAILNET_API}/tailnet/-/acl",
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
        provider_http.release(exc)
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
            provider_http.condition("Ready", True, "Reconciled", "The policy is as declared.")
            if tested
            else provider_http.condition(
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
    token = tailnet_api.tailnet_token(spec.get("connection_ref", ""))
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
            provider_http.condition("Ready", True, "Reconciled", "The policy is as declared.")
        ],
        message="Tailnet policy applied after its own tests passed.",
    )


def _tailnet_policy(token: str) -> dict[str, Any]:
    """The tailnet's policy file, as Tailscale currently holds it."""

    try:
        with provider_http.open_url(
            f"{tailnet_api.TAILNET_API}/tailnet/-/acl",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=30,
        ) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        provider_http.release(exc)
        raise tailnet_api.tailnet_refused("the policy read", "policy_file:read", exc.code) from exc
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
        with provider_http.open_url(
            f"{tailnet_api.TAILNET_API}/tailnet/-/acl/preview"
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
        provider_http.release(exc)
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
    # empty list they would all read the same: a successful sweep of a
    # tailnet with no policy, with nothing unreachable and nothing saying why.
    token = tailnet_api.tailnet_token("")
    policy = _tailnet_policy(token)
    parts = tailnet_api.tailnet_parts(
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


def reach_by_device(devices: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
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
        token = tailnet_api.tailnet_token("")
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
