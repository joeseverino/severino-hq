"""Cloudflare redirects as records: which names a rule matches and where it sends them.

``read`` walks each zone through the calls ``providers`` hands it. A rule's names come from
its expression (``http.host`` comparisons and URL literals); a page rule's from
its URL pattern. A target is a static URL or an expression; its host is the
first URL literal it names.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from control_plane.names import normalized_hostname

# The dynamic redirect phase: zone Single Redirects.
REDIRECT_PHASE = "http_request_dynamic_redirect"

_HOST_FIELD = re.compile(
    r"http\.host\s*(?:eq|==|in|contains|wildcard|strict\s+wildcard)\s*(\{[^}]*\}|r?\"[^\"]*\")"
)
_QUOTED = re.compile(r"\"([^\"]*)\"")
_URL = re.compile(r"https?://([A-Za-z0-9*.-]+)")


def _host(text: str) -> str:
    """A hostname from a pattern: scheme, path and a leading ``*`` or ``*.`` removed."""

    value = str(text or "").strip()
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0].split(":", 1)[0]
    wildcard = value.startswith("*.")
    value = value.lstrip("*").lstrip(".")
    name = normalized_hostname(value)
    if not name or "*" in name:
        return ""
    return f"*.{name}" if wildcard else name


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def expression_hosts(expression: str) -> tuple[str, ...]:
    """The hostnames a rule expression matches, where it names them."""

    text = str(expression or "")
    found = [
        _host(name)
        for match in _HOST_FIELD.finditer(text)
        for name in _QUOTED.findall(match.group(1))
    ]
    found.extend(_host(match.group(1)) for match in _URL.finditer(text))
    return _unique(found)


def target_host(target: str) -> str:
    """The host a redirect target names: a URL's, or an expression's first URL literal."""

    text = str(target or "")
    if text.startswith(("http://", "https://")):
        return _host(urlsplit(text).hostname or "")
    match = _URL.search(text)
    return _host(match.group(1)) if match else ""


def rule_record(rule: Mapping[str, Any], *, zone: str) -> dict[str, Any] | None:
    """One redirect rule as a record, or None for a rule that does not redirect."""

    if rule.get("action") != "redirect":
        return None
    parameters = (rule.get("action_parameters") or {}).get("from_value") or {}
    target_url = parameters.get("target_url") or {}
    target = str(target_url.get("value") or target_url.get("expression") or "")
    return {
        "zone": zone,
        "source": "rule",
        "id": str(rule.get("id") or ""),
        "description": str(rule.get("description") or ""),
        "hostnames": expression_hosts(str(rule.get("expression") or "")),
        "target": target,
        "target_host": target_host(target),
        "status_code": parameters.get("status_code"),
        "preserve_query_string": bool(parameters.get("preserve_query_string")),
        "enabled": bool(rule.get("enabled", True)),
    }


def page_rule_record(rule: Mapping[str, Any], *, zone: str) -> dict[str, Any] | None:
    """One forwarding page rule as a record, or None for a rule that does not forward."""

    forwarding = next(
        (
            action.get("value") or {}
            for action in rule.get("actions") or ()
            if action.get("id") == "forwarding_url"
        ),
        None,
    )
    if forwarding is None:
        return None
    patterns = [
        str((target.get("constraint") or {}).get("value") or "")
        for target in rule.get("targets") or ()
        if target.get("target") == "url"
    ]
    target = str(forwarding.get("url") or "")
    return {
        "zone": zone,
        "source": "page_rule",
        "id": str(rule.get("id") or ""),
        "hostnames": _unique(_host(pattern) for pattern in patterns),
        "target": target,
        "target_host": target_host(target),
        "status_code": forwarding.get("status_code"),
        "enabled": str(rule.get("status") or "active") == "active",
    }


@dataclass(frozen=True)
class ZoneReads:
    """The Cloudflare calls a redirect read makes, supplied by the controller."""

    zones: Callable[[str], list[dict[str, Any]]]
    listed: Callable[[str, str], list[dict[str, Any]]]
    result: Callable[[str, str], Any]
    reason: Callable[[BaseException], str]
    error: type[Exception]
    # Reports one declared part refused on a zone: (part, exception, scope, ref).
    refuse: Callable[..., None] = lambda part, exc, **where: None


def _rules(api: ZoneReads, zone_id: str, zone: str, ref: str) -> list[dict[str, Any]]:
    """Redirect rules in the zone's dynamic redirect phase rulesets."""

    found: list[dict[str, Any]] = []
    for ruleset in api.listed(f"/zones/{zone_id}/rulesets", ref):
        if ruleset.get("phase") != REDIRECT_PHASE:
            continue
        detail = api.result(f"/zones/{zone_id}/rulesets/{ruleset.get('id', '')}", ref)
        for rule in (detail or {}).get("rules") or ():
            record = rule_record(rule, zone=zone)
            if record is not None:
                found.append(record)
    return found


def _page_rules(api: ZoneReads, zone_id: str, zone: str, ref: str) -> list[dict[str, Any]]:
    """Forwarding page rules."""

    rules = api.result(f"/zones/{zone_id}/pagerules", ref) or ()
    return [
        record
        for rule in rules
        if isinstance(rule, dict) and (record := page_rule_record(rule, zone=zone)) is not None
    ]


# The reading's declared parts (``cloudflare.redirect``), each with its reader.
_PARTS = (("rules", _rules), ("page_rules", _page_rules))


def read(refs: Iterable[str], api: ZoneReads) -> list[dict[str, Any]]:
    """Every redirect on every zone each credential sees.

    A part refused on one zone is reported through ``api.refuse`` on that zone;
    every part refused on every zone is a refused read and raises.
    """

    found: list[dict[str, Any]] = []
    for ref in refs:
        zones = [zone for zone in api.zones(ref) if zone.get("name")]
        refused: list[Exception] = []
        for zone in zones:
            found.extend(_zone(api, ref, zone, refused))
        if zones and len(refused) == len(_PARTS) * len(zones):
            raise refused[0]
    return found


def _zone(api: ZoneReads, ref: str, zone: Mapping[str, Any], refused: list) -> list[dict[str, Any]]:
    name = str(zone["name"]).strip().lower().rstrip(".")
    base = {"connection_ref": ref, "account_id": str((zone.get("account") or {}).get("id") or "")}
    found: list[dict[str, Any]] = []
    for part, reader in _PARTS:
        try:
            found.extend({**base, **record} for record in reader(api, str(zone.get("id", "")), name, ref))
        except (api.error, OSError, ValueError) as exc:
            api.refuse(part, exc, scope=name, connection_ref=ref)
            refused.append(exc if isinstance(exc, api.error) else api.error(api.reason(exc)))
    return found
