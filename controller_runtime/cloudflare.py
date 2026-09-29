"""Cloudflare: DNS records and zones, their posture, and the connection probes.

The API client is ``cloudflare_api``'s; account-wide readings are
``cloudflare_account``'s.
"""

from __future__ import annotations

from typing import Any

from control_plane.names import normalized_hostname
from control_plane.provider_adapters.cloudflare import (
    DNS_RECORD_KIND,
    ZONE_KIND,
    caa_parts,
    normalized_record_content,
)
from control_plane.provider_adapters.contracts import ProviderError, ProviderResult
from control_plane.provider_adapters.parts import refuse_part
from .handlers import acts, lists, probes
from . import cloudflare_analytics, cloudflare_api, connection_env, provider_http


def _cloudflare_zones() -> list[dict[str, Any]]:
    return provider_http.snapshot_value(("cloudflare-zones",), lambda: cloudflare_api.cloudflare_paged("/zones"))


_ZONE_IDS: dict[str, str] = {}


def _cloudflare_zone_id(zone: str) -> str:
    wanted = zone.strip().lower().rstrip(".")
    if wanted in _ZONE_IDS:
        return _ZONE_IDS[wanted]
    for candidate in _cloudflare_zones():
        name = str(candidate.get("name", "")).strip().lower()
        if name:
            _ZONE_IDS[name] = candidate["id"]
    if wanted not in _ZONE_IDS:
        raise ProviderError(
            f"The Cloudflare credential cannot see a zone called {wanted!r}."
        )
    return _ZONE_IDS[wanted]


def _cloudflare_records(zone_id: str) -> list[dict[str, Any]]:
    return cloudflare_api.cloudflare_paged(f"/zones/{zone_id}/dns_records")


def _caa_data(content: str) -> dict[str, Any]:
    """A CAA value in the three-field shape Cloudflare will accept.

    Split by the same parser the spec validates with, so a value the form
    accepted cannot be one this refuses.
    """

    parts = caa_parts(content)
    if parts is None:
        raise ProviderError('A CAA value must look like: 0 issue "letsencrypt.org".')
    flags, tag, value = parts
    return {"flags": flags, "tag": tag, "value": value}


def _cloudflare_payload(spec: dict[str, Any]) -> dict[str, Any]:
    record_type = str(spec["record_type"]).upper()
    payload: dict[str, Any] = {
        "type": record_type,
        "name": normalized_hostname(spec["name"]),
        "ttl": int(spec.get("ttl", 1) or 1),
    }
    if record_type == "CAA":
        payload["data"] = _caa_data(str(spec["content"]))
    else:
        payload["content"] = normalized_record_content(
            record_type, str(spec["content"])
        )
    if record_type == "MX":
        payload["priority"] = int(spec.get("priority") or 0)
    if record_type in {"A", "AAAA", "CNAME"}:
        # Sent only for the types that can carry it. Cloudflare rejects the
        # field outright on a TXT or MX record rather than ignoring it.
        payload["proxied"] = bool(spec.get("proxied", False))
    return payload


def _record_matches(live: dict[str, Any], spec: dict[str, Any]) -> bool:
    record_type = str(spec["record_type"]).upper()
    if str(live.get("type", "")).upper() != record_type:
        return False
    if normalized_hostname(live.get("name", "")) != normalized_hostname(spec["name"]):
        return False
    return normalized_record_content(
        record_type, str(live.get("content", ""))
    ) == normalized_record_content(record_type, str(spec["content"]))


def _record_status(zone: str, live: dict[str, Any]) -> dict[str, Any]:
    return {
        "zone": zone,
        # Carried so the next reconciliation can find this exact record even if
        # its name, type or value were all edited at once. Without it, an edit
        # that changes the value looks like a brand new record and the old one
        # is left behind, answering, with nothing in HQ pointing at it.
        "record_id": live.get("id", ""),
        "name": live.get("name", ""),
        "record_type": str(live.get("type", "")).upper(),
        "content": live.get("content", ""),
        "priority": live.get("priority"),
        "proxied": bool(live.get("proxied", False)),
        "ttl": live.get("ttl", 1),
    }


@acts(DNS_RECORD_KIND, "reconcile")
def reconcile_cloudflare_record(
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    """Make one public DNS record match its declaration.

    Identity is the recorded Cloudflare record id where there is one, and the
    name/type/value triple otherwise. That order matters: a zone apex commonly
    holds several records of one type, so matching by name alone would edit
    whichever of four CAA records happened to come back first.
    """

    zone = str(spec["zone"]).strip().lower().rstrip(".")
    zone_id = _cloudflare_zone_id(zone)
    records = _cloudflare_records(zone_id)
    desired = _cloudflare_payload(spec)

    record_id = str((observed or {}).get("record_id", "")).strip()
    live = next((item for item in records if item.get("id") == record_id), None)
    if live is None:
        live = next((item for item in records if _record_matches(item, spec)), None)

    if live is None:
        if apply:
            live = cloudflare_api.cloudflare_request(
                f"/zones/{zone_id}/dns_records", method="POST", payload=desired
            )
        return ProviderResult(
            changed=True,
            status=_record_status(zone, live or {}),
            conditions=[
                provider_http.condition("Ready", True, "Created", "DNS record was created.")
            ],
            message="Public DNS record created.",
        )

    current = {
        "type": str(live.get("type", "")).upper(),
        "name": normalized_hostname(live.get("name", "")),
        "ttl": int(live.get("ttl", 1) or 1),
    }
    if desired["type"] == "CAA":
        current["data"] = {
            key: (live.get("data") or {}).get(key) for key in ("flags", "tag", "value")
        }
    else:
        current["content"] = normalized_record_content(
            desired["type"], str(live.get("content", ""))
        )
    if desired["type"] == "MX":
        current["priority"] = int(live.get("priority") or 0)
    if "proxied" in desired:
        current["proxied"] = bool(live.get("proxied", False))

    if current == desired:
        return ProviderResult(
            changed=False,
            status=_record_status(zone, live),
            conditions=[
                provider_http.condition("Ready", True, "Reconciled", "DNS record is current.")
            ],
            message="Public DNS record unchanged.",
        )
    if apply:
        live = cloudflare_api.cloudflare_request(
            f"/zones/{zone_id}/dns_records/{live['id']}",
            method="PUT",
            payload=desired,
        )
    return ProviderResult(
        changed=True,
        status=_record_status(zone, live or {}),
        conditions=[provider_http.condition("Ready", True, "Reconciled", "DNS record was updated.")],
        message="Public DNS record updated.",
    )


@acts(DNS_RECORD_KIND, "delete")
def delete_cloudflare_record(
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    """Remove one record, treating an already-absent one as success.

    Only ever the single record this declaration owns. Cloudflare deletes by id,
    which is the one safe way to do this: a zone apex may hold nine records, and
    a delete that matched on name would take the other eight with it.
    """

    zone = str(spec["zone"]).strip().lower().rstrip(".")
    zone_id = _cloudflare_zone_id(zone)
    records = _cloudflare_records(zone_id)

    record_id = str((observed or {}).get("record_id", "")).strip()
    live = next((item for item in records if item.get("id") == record_id), None)
    if live is None:
        live = next((item for item in records if _record_matches(item, spec)), None)
    if live is None:
        return ProviderResult(
            changed=False,
            status={"zone": zone, "name": spec.get("name", ""), "removed": True},
            conditions=[
                provider_http.condition("Ready", True, "Absent", "No such record in Cloudflare.")
            ],
            message="Public DNS record was already absent.",
        )
    if apply:
        cloudflare_api.cloudflare_request(
            f"/zones/{zone_id}/dns_records/{live['id']}", method="DELETE"
        )
    return ProviderResult(
        changed=True,
        status={"zone": zone, "name": spec.get("name", ""), "removed": True},
        conditions=[provider_http.condition("Ready", True, "Removed", "DNS record was removed.")],
        message="Public DNS record removed.",
    )


# The zone settings worth carrying: how a domain answers over TLS. Named rather
# than taken whole, because the settings endpoint returns eighty entries and
# most of them (minify, rocket loader, browser cache TTL) are not posture.
ZONE_POSTURE_SETTINGS = (
    "ssl",
    "min_tls_version",
    "tls_1_3",
    "always_use_https",
    "automatic_https_rewrites",
)


def _registrar_domains() -> dict[str, dict[str, Any]]:
    """What the registrar holds for every domain on the account, by name.

    Read from Cloudflare rather than from RDAP. RDAP can only answer *when* a
    domain expires; the registrar knows whether it will renew itself, and that
    is the fact worth acting on. A domain three months out with auto-renew on is
    a date; the same domain with auto-renew off is an outage with a countdown,
    and nothing else in HQ would know the difference.

    The account credential carries the registration surface. A refusal is the
    zone sweep's refused "registration" part, for every zone.
    """

    try:
        account = cloudflare_analytics.analytics_account()
        domains = cloudflare_api.cloudflare_api_cursor_list(
            f"/accounts/{account}/registrar/registrations"
        )
    except (ProviderError, OSError, ValueError) as exc:
        refuse_part("registration", exc)
        return {}
    found: dict[str, dict[str, Any]] = {}
    for domain in domains:
        name = str(domain.get("domain_name", "")).strip().lower().rstrip(".")
        if not name:
            continue
        found[name] = {
            "expires_at": str(domain.get("expires_at", ""))[:10],
            "auto_renew": bool(domain.get("auto_renew")),
            "locked": bool(domain.get("locked")),
            "status": str(domain.get("status", "")),
            "registrar": "Cloudflare",
        }
    return found


def _cloudflare_zone_posture(zone_id: str, zone: str = "") -> dict[str, str]:
    """How a zone answers over TLS, read through the credential that can see it.

    The DNS token cannot: it holds records and nothing else. `cloudflare_api`
    carries the account surface, zone settings included.

    One request per setting: the batch settings endpoint is deprecated. A
    failure here is not a failed sweep: the zone still reports its records, and
    a setting refused is the zone's refused "posture" part.
    """

    if not zone_id:
        return {}
    found: dict[str, str] = {}
    for setting in ZONE_POSTURE_SETTINGS:
        try:
            envelope = cloudflare_api.cloudflare_api_request(f"/zones/{zone_id}/settings/{setting}")
        except (ProviderError, OSError, ValueError) as exc:
            refuse_part("posture", exc, scope=zone)
            return {}
        item = (envelope or {}).get("result")
        if isinstance(item, dict) and item.get("value") not in (None, ""):
            found[setting] = str(item["value"])
    return found


@lists(ZONE_KIND)
def list_cloudflare_zones() -> list[dict[str, Any]]:
    """Every zone the credential can see, declared or not.

    Reported in full deliberately: which of them HQ should manage is an
    operator's decision, and it cannot be made on a screen that only lists the
    ones already decided about.
    """

    connection_ref = provider_http.required(connection_env.connection_prefix("cloudflare_dns"), "CONNECTION_REF")
    # Read once for the whole sweep rather than once per zone: it is one list
    # for the account, and asking per zone would be four calls for one answer.
    registrars = _registrar_domains()
    return [
        {
            "zone": zone["name"],
            "connection_ref": connection_ref,
            "account_id": str((zone.get("account") or {}).get("id") or ""),
            "status": zone.get("status", ""),
            "plan": (zone.get("plan") or {}).get("name", ""),
            "posture": _cloudflare_zone_posture(
                str(zone.get("id", "")), str(zone["name"]).strip().lower().rstrip(".")
            ),
            "registration": registrars.get(
                str(zone.get("name", "")).strip().lower().rstrip("."), {}
            ),
        }
        for zone in _cloudflare_zones()
        if zone.get("name")
    ]


@lists(DNS_RECORD_KIND)
def list_cloudflare_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for zone in _cloudflare_zones():
        zone_name = zone.get("name")
        if not zone_name:
            continue
        for live in _cloudflare_records(zone["id"]):
            if not live.get("type") or not live.get("name"):
                continue
            records.append(_record_status(zone_name, live))
    return records


# ----- Connections -----------------------------------------------------------
#
# What the controller can reach, and whether it still can. The rendered
# environment is the inventory, so this enumerates itself: a 1Password item
# becomes a row here, a probe and (once HQ has been told) a row on a page,
# without anything in this file naming it.


# A probe answers two questions in one call: whether the credential still works,
# and what it reaches. The second is why the connection sweep is worth running
# at all: a Portainer knows which machines exist, a DNS token knows which zones
# it may touch, and both are facts HQ can only get by asking. Reported as names
# so every menu that offers "which machine" or "which domain" is derived from
# the credential that would have to carry out the answer.


def _token_expiry(verification: Any) -> str:
    """The ``expires_on`` a ``/user/tokens/verify`` envelope reports, or ""."""

    result = verification.get("result") if isinstance(verification, dict) else None
    return str((result or {}).get("expires_on") or "") if isinstance(result, dict) else ""


@probes("cloudflare_dns")
def _probe_cloudflare_dns(connection_ref: str) -> dict[str, Any]:
    verification = cloudflare_api.cloudflare_envelope(
        "/user/tokens/verify", connection_ref=connection_ref
    )
    # Which zones *matter* is not the controller's to know. The credential
    # reports what it can reach; HQ declares which zones it is responsible for
    # and is the only side able to compare the two.
    zones = cloudflare_api.cloudflare_envelope(
        "/zones?per_page=50", connection_ref=connection_ref
    ).get("result")
    names = sorted(
        zone["name"]
        for zone in zones or ()
        if isinstance(zone, dict) and zone.get("name")
    )
    return {
        "detail": f"{len(names)} zones.",
        "reaches": names,
        "expires_at": _token_expiry(verification),
    }


@probes("cloudflare_api")
def _probe_cloudflare_api(connection_ref: str) -> dict[str, Any]:
    """Prove the account credential answers, and report the sites it observes.

    ``reaches`` is the analytics sites rather than the zones, because that is
    what distinguishes this credential from the DNS one beside it: both see the
    same zones, and a page listing them twice tells an operator nothing about
    which connection is which.

    A site with no ruleset bound to a hostname is left out. Cloudflare keeps a
    Web Analytics site around after whatever it was attached to goes away, so
    the account carries entries that describe nothing, and a site HQ cannot
    name a host for is one it could not join to anything either.
    """

    verification = cloudflare_api.cloudflare_api_request("/user/tokens/verify", connection_ref)
    if not isinstance(verification, dict) or not verification.get("success"):
        raise ProviderError("Cloudflare token verification failed.")

    account = cloudflare_analytics.analytics_account(connection_ref)
    hosts = sorted(site["host"] for site in cloudflare_analytics.account_sites(account, connection_ref))
    measured = "site" if len(hosts) == 1 else "sites"
    return {
        "detail": f"{len(hosts)} analytics {measured}.",
        "reaches": hosts,
        "expires_at": _token_expiry(verification),
    }
