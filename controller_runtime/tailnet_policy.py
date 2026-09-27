"""The tailnet policy's own tests, as the ratchet a declared policy must clear.

Tailscale's validation runs only the tests a document carries, so it is a gate
only when those tests check at least what the live policy's tests check.
"""

from __future__ import annotations

from typing import Any

from control_plane.provider_adapters.contracts import ProviderError


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
