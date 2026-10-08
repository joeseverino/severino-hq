"""Everything HQ derives about the tailnet, for every adapter.

The tailnet page, the HTTP API, MCP, the CLI and the SDK read this one
projection: the policy's settings, grants, shell rules, groups and tags with
the machines they stand for, services, app connectors and policy tests, the
findings raised about the tailnet, and each tailnet reading HQ could not read
with the reason. None of them derives a fact the others cannot return.
"""

from dataclasses import asdict, dataclass, replace
from typing import Any
from urllib.parse import urlencode

from hq.domains.control_plane.provider_adapters.tailscale import TAILNET_KIND, TAILNET_POLICY_KIND
from hq.domains.control_plane.providers import PROVIDERS

from .credential_sight import READABLE, Sight, credential_sight
from .entity_links import EntityLink
from .finding_model import Finding
from .findings import estate_findings, serialize_finding
from .policy_links import PolicyName, PolicyNames, tagged
from .projection import projection_scope
from .routes import reverse
from .security import Capability, Principal
from .tailnet import (
    NEW_DEVICES,
    RESOLVES_THROUGH,
    Policy,
    declaration,
    devices,
    grant_ports,
    policy,
)
from .topology import derive_topology

TAILNET_KINDS = (TAILNET_KIND, TAILNET_POLICY_KIND)


def tailnet_providers() -> frozenset[str]:
    """The connection providers whose credentials read the tailnet."""

    return frozenset(
        provider for kind in TAILNET_KINDS for provider in PROVIDERS[kind].connection_providers
    )


@dataclass(frozen=True)
class NamedRule:
    """A grant or shell rule, and the machines its sources and destinations name."""

    rule: dict
    sources: tuple[PolicyName, ...]
    destinations: tuple[PolicyName, ...]


@dataclass(frozen=True)
class Setting:
    """One tailnet setting; ``addresses`` when its value names machines."""

    label: str
    value: str
    addresses: tuple[PolicyName, ...] | None = None
    # The findings page filtered to the rule this setting breaks, when it does.
    problem_url: str = ""


@dataclass(frozen=True)
class TagRow:
    tag: dict
    devices: tuple[EntityLink, ...]
    # Devices were read and none of them has this tag.
    unworn: bool = False


@dataclass(frozen=True)
class TailnetContext:
    policy: Policy
    declaration: str
    settings: tuple[Setting, ...]
    grants: tuple[NamedRule, ...]
    ssh_rules: tuple[NamedRule, ...]
    tags: tuple[TagRow, ...]
    findings: tuple[Finding, ...]
    # Tailnet readings the credential could not read, and why.
    unread: tuple[Sight, ...]
    credential_refusal: str

    @property
    def known(self) -> bool:
        return self.policy.known

    def as_dict(self) -> dict[str, Any]:
        """The derived facts, for the API, MCP, CLI and SDK."""

        found = self.policy
        return {
            "known": self.known,
            "declaration": self.declaration or None,
            "counts": {
                "grants": len(found.grants),
                "groups": len(found.groups),
                "tags": len(found.tags),
                "tests": len(found.tests),
            },
            "settings": [
                {
                    "label": item.label,
                    "value": item.value,
                    "machines": _names(item.addresses) if item.addresses else None,
                }
                for item in self.settings
            ],
            "grants": [_rule(item, ports=True) for item in self.grants],
            "ssh_rules": [_rule(item) for item in self.ssh_rules],
            "groups": [
                {"name": group.get("name", ""), "members": list(group.get("members") or ())}
                for group in found.groups
            ],
            "tags": [
                {
                    "name": row.tag.get("name", ""),
                    "owners": list(row.tag.get("owners") or ()),
                    "machines": [asdict(link) for link in row.devices],
                }
                for row in self.tags
            ],
            "services": [dict(service) for service in found.services],
            "app_connectors": [dict(connector) for connector in found.app_connectors],
            "tests": [dict(test) for test in found.tests],
            "locked_out": list(found.locked_out),
            "findings": [serialize_finding(finding) for finding in self.findings],
            "unread": [_unread(sight) for sight in self.unread],
            "credential_refusal": self.credential_refusal or None,
        }


def _names(names: tuple[PolicyName, ...]) -> list[dict[str, Any]]:
    return [
        {"text": name.text, "link": asdict(name.link) if name.link else None} for name in names
    ]


def _rule(item: NamedRule, *, ports: bool = False) -> dict[str, Any]:
    shown = {
        key: value for key, value in item.rule.items() if key not in ("src", "dst", "ports")
    }
    if ports:
        shown["ports"] = [
            {"entry": entry, "name": name} for entry, name in item.rule.get("ports") or ()
        ]
    return {
        **shown,
        "src": list(item.rule.get("src") or ()),
        "dst": list(item.rule.get("dst") or ()),
        "sources": _names(item.sources),
        "destinations": _names(item.destinations),
    }


def _unread(sight: Sight) -> dict[str, Any]:
    return {
        "kind": sight.kind,
        "label": sight.label,
        "state": sight.state,
        "state_label": sight.state_label,
        "refusal": sight.refusal or None,
        "error": sight.error or None,
        "remedy": sight.remedy or None,
        "requires": sight.requires or None,
    }


def _findings(principal: Principal) -> tuple[Finding, ...]:
    """Findings whose subject is a tailnet connection or declaration, or a tailnet kind."""

    topology = derive_topology(principal=principal)
    providers = tailnet_providers()
    subjects = {
        node.id
        for node in topology.nodes
        if (node.kind == "connection" and node.provider in providers)
        or node.kind_key in TAILNET_KINDS
    }
    return tuple(
        finding
        for finding in estate_findings(principal=principal)
        if finding.subject in subjects or finding.scope in TAILNET_KINDS
    )


# The setting row each rule is about.
_SETTING_RULES = {"devices-join-without-approval": NEW_DEVICES}


def _setting_problems(findings: tuple[Finding, ...]) -> dict[str, str]:
    """Each setting row an open finding is about, and the findings page for that rule."""

    found: dict[str, str] = {}
    for finding in findings:
        label = _SETTING_RULES.get(finding.rule)
        if label:
            found[label] = f"{reverse('control_plane:findings')}?{urlencode({'rule': finding.rule})}"
    return found


def _sight() -> tuple[tuple[Sight, ...], str]:
    """Tailnet readings not read, and the provider's refusal of the credential."""

    providers = tailnet_providers()
    unread: list[Sight] = []
    refusal = ""
    for found in credential_sight():
        if found.provider not in providers:
            continue
        refusal = refusal or found.credential_refusal
        unread.extend(item for item in found.sights if item.state != READABLE)
    return tuple(unread), refusal


def tailnet_context(*, principal: Principal) -> TailnetContext:
    """Every fact HQ derives about the tailnet, derived once."""

    principal.require(Capability.READ)
    with projection_scope():
        found = policy()
        found = replace(found, grants=grant_ports(found.grants))
        names = PolicyNames(hosts=found.hosts)
        known = devices()
        carried = tagged(known, names)
        worn = {tag for device in known.values() for tag in device.tags}
        unread, refusal = _sight()
        findings = _findings(principal)
        problems = _setting_problems(findings)
        return TailnetContext(
            policy=found,
            declaration=declaration(),
            settings=tuple(
                Setting(
                    label,
                    value,
                    names.addresses(value.split(", ")) if label == RESOLVES_THROUGH else None,
                    problems.get(label, ""),
                )
                for label, value in found.facts
            ),
            grants=tuple(
                NamedRule(rule, names.of(rule.get("src") or ()), names.of(rule.get("dst") or ()))
                for rule in found.grants
            ),
            ssh_rules=tuple(
                NamedRule(rule, names.of(rule.get("src") or ()), names.of(rule.get("dst") or ()))
                for rule in found.ssh_rules
            ),
            tags=tuple(
                TagRow(
                    tag,
                    carried.get(tag.get("name"), ()),
                    unworn=bool(known) and tag.get("name") not in worn,
                )
                for tag in found.tags
            ),
            findings=findings,
            unread=unread,
            credential_refusal=refusal,
        )


def get_tailnet(*, principal: Principal) -> dict[str, Any]:
    """The tailnet as one item, for the registered read."""

    found = tailnet_context(principal=principal).as_dict()
    return {"items": [found], "count": 1}
