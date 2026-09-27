"""Policy amendments a finding offers, written through the gated policy kind.

Each re-derives what to change from the declared policy itself, never from the
caller, so the only amendment it can produce is the one the finding describes.
The write is the ordinary declaration write: the tailnet policy is a gated
kind, so a person consents before anything reaches the tailnet, and the
controller applies it only through a connection that manages.
"""

from __future__ import annotations

import json
from typing import Any

from .security import Capability, Principal
from .ui import counted

# Where a rule names who it admits and what it admits to.
_RULE_SIDES = ("src", "dst")


def empty_granted_groups(parsed: dict[str, Any]) -> tuple[str, ...]:
    """Groups with no members that a grant or shell rule names."""

    groups = parsed.get("groups") or {}
    named = {
        name
        for rule in (*(parsed.get("grants") or ()), *(parsed.get("ssh") or ()))
        for side in _RULE_SIDES
        for name in rule.get(side) or ()
    }
    return tuple(sorted(name for name, members in groups.items() if not members and name in named))


def _names(value: Any) -> set[str]:
    """Every string in a policy section, keys included."""

    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return {*value, *(name for item in value.values() for name in _names(item))}
    if isinstance(value, list):
        return {name for item in value for name in _names(item)}
    return set()


def _referenced_outside_rules(parsed: dict[str, Any], group: str) -> list[str]:
    """Top-level sections other than groups, grants and ssh that name ``group``."""

    return sorted(
        key
        for key, value in parsed.items()
        if key not in {"groups", "grants", "ssh"} and group in _names(value)
    )


def _without(rules: list[dict[str, Any]], groups: set[str]) -> tuple[list[dict[str, Any]], int]:
    """Rules with ``groups`` struck from each side; a rule left with an empty side
    admitted nobody, and is dropped. Returns the rules and how many were dropped."""

    kept: list[dict[str, Any]] = []
    dropped = 0
    for rule in rules:
        amended = dict(rule)
        for side in _RULE_SIDES:
            if side in amended:
                amended[side] = [name for name in amended[side] or () if name not in groups]
        if any(side in rule and not amended[side] for side in _RULE_SIDES):
            dropped += 1
            continue
        kept.append(amended)
    return kept, dropped


def policy_without_empty_groups(document: str) -> tuple[str, str]:
    """The policy with every empty granted group removed, and what moved.

    ``("", "")`` when there is none. Raises ``ValueError`` when a group is also
    named somewhere this does not edit (a tag owner, an auto-approver, a
    test), because removing it there changes what the policy means.
    """

    parsed = json.loads(document)
    empty = set(empty_granted_groups(parsed))
    if not empty:
        return "", ""
    for group in sorted(empty):
        elsewhere = _referenced_outside_rules(parsed, group)
        if elsewhere:
            raise ValueError(
                f"{group} is also named in {', '.join(elsewhere)}. Edit the policy by hand."
            )
    parsed["groups"] = {
        name: members for name, members in (parsed.get("groups") or {}).items()
        if name not in empty
    }
    dropped = 0
    for section in ("grants", "ssh"):
        if section in parsed:
            parsed[section], count = _without(list(parsed[section] or ()), empty)
            dropped += count
    summary = f"removed {', '.join(sorted(empty))}"
    if dropped:
        summary += f" and {counted(dropped, 'rule')} that admitted only them"
    return json.dumps(parsed), summary


def request_empty_groups_removal(
    command,
    *,
    principal: Principal,
    current_key: str,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Remove the empty groups the policy still grants, as a proposed policy change."""

    from control_plane.models import ManagedResource

    from .infrastructure import (
        ManagedResourceCommand,
        NotFoundError,
        PolicyError,
        save_managed_resource,
    )
    from .tailnet import POLICY_KIND

    del command
    principal.require(Capability.MANAGE_INFRASTRUCTURE)
    policy = ManagedResource.objects.filter(key=current_key, kind=POLICY_KIND).first()
    if policy is None:
        raise NotFoundError(f"No tailnet policy is declared as {current_key!r}.")
    try:
        document, summary = policy_without_empty_groups(str(policy.spec.get("document", "")))
    except ValueError as exc:
        raise PolicyError(str(exc)) from exc
    if not document:
        raise PolicyError("The declared policy grants no empty group.")
    result = save_managed_resource(
        ManagedResourceCommand(
            key=policy.key,
            kind=policy.kind,
            spec={**policy.spec, "document": document},
            enabled=policy.enabled,
        ),
        principal=principal,
        current_key=policy.key,
        expected_updated_at=expected_updated_at,
    )
    return {**result, "change": summary}
