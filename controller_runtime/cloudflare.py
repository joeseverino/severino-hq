"""Cloudflare: DNS records and zones, and every reading taken through its API."""

from __future__ import annotations

from collections.abc import Callable
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from control_plane.names import normalized_hostname
from control_plane.provider_adapters.cloudflare import (
    caa_parts,
    normalized_record_content,
)
from control_plane.provider_adapters.contracts import (
    CREDENTIAL_REFUSAL,
    NETWORK_FAILURE,
    ProviderError,
    ProviderResult,
    cloudflare_refusal,
)
from control_plane.provider_adapters.parts import (
    refuse_part,
    unread_reason as _unread_reason,
)
from controller_runtime import redirects
from .handlers import reads
from . import cloudflare_analytics, connection_env, provider_http


# ----- Cloudflare ------------------------------------------------------------
#
# Two credentials, each scoped to one surface.
#
# `cloudflare_dns` is deliberately narrow: it can read the zones on the account
# and read and write their DNS records, and nothing else. Zone settings,
# analytics and every account-level surface answer 403 to it. That is why the
# zone provider declares no reconcile it could perform (see the capability
# registry) and why nothing here reaches for a setting it cannot change.
#
# `cloudflare_api` carries the account surface (analytics, zone settings,
# registration) and reaches no DNS record. Neither is a subset of the other,
# so a provider states which one it needs and gets exactly that.


CLOUDFLARE_API_URL = "https://api.cloudflare.com/client/v4"


def _cloudflare_url(
    connection_ref: str = "", *, provider: str = "cloudflare_dns"
) -> str:
    """The API base; <PREFIX>_URL overrides the public one."""

    prefix = connection_env.connection_prefix(provider, connection_ref)
    return (os.environ.get(f"{prefix}_URL", "").strip() or CLOUDFLARE_API_URL).rstrip("/")


def _cloudflare_token(
    connection_ref: str = "", *, provider: str = "cloudflare_dns"
) -> str:
    return provider_http._required(connection_env.connection_prefix(provider, connection_ref), "API_TOKEN")


def _cloudflare_envelope(
    path: str,
    *,
    method: str = "GET",
    payload: Any = None,
    provider: str = "cloudflare_dns",
    connection_ref: str = "",
) -> dict[str, Any]:
    """One Cloudflare call, returning the whole envelope with its errors kept.

    Not routed through ``_request`` because Cloudflare says something useful in
    the body of a 400: "Content for A record must be a valid IPv4 address",
    "An identical record already exists", and the shared helper turns every
    non-200 into the same sentence. A rejected DNS change that only says
    "Provider request failed: HTTPError" is a support ticket to yourself.

    ``success`` is checked here rather than by each caller, because Cloudflare
    also answers 200 with ``success: false``: a token missing one permission
    returns no ``result`` at all, and a list helper reading ``result`` off that
    collects nothing and reports an empty estate. An account that looks empty
    and an account that refused to answer must not read the same.

    Both credentials come through here: the zone-scoped DNS token and the
    account-scoped analytics one differ only in which connection names them,
    which is what ``provider`` and ``connection_ref`` select.
    """

    prefix = connection_env.connection_prefix(provider, connection_ref)
    _cloudflare_breaker(prefix)
    url = f"{_cloudflare_url(connection_ref, provider=provider)}{path}"
    headers = {
        "Authorization": (
            f"Bearer {_cloudflare_token(connection_ref, provider=provider)}"
        ),
        "Accept": "application/json",
    }
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    try:
        with provider_http._open(url, data=body, headers=headers, method=method) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        with exc:
            detail = _cloudflare_errors(exc.read())
        raise _cloudflare_refused(
            prefix,
            f"Cloudflare refused the request: {detail}",
            detail,
            status=exc.code,
            verified=lambda: _cloudflare_verified(provider, connection_ref),
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ProviderError(
            f"Cloudflare request failed: {type(exc).__name__}.",
            failure=NETWORK_FAILURE,
        ) from exc
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise ProviderError("Cloudflare returned invalid JSON.") from exc
    if not parsed.get("success", False):
        detail = _cloudflare_errors(raw)
        raise _cloudflare_refused(
            prefix, f"Cloudflare refused the request: {detail}", detail
        )
    return parsed if isinstance(parsed, dict) else {}


def _refused_credentials() -> dict[str, str]:
    """Credentials refused outright during this sweep, by connection prefix."""

    snapshot = provider_http._PROVIDER_SNAPSHOT.get()
    if snapshot is None:
        return {}
    return snapshot.setdefault(("refused-credentials",), {})


def _cloudflare_breaker(prefix: str) -> None:
    """Raise without a call when this sweep has already seen the credential refused.

    Every further call with a refused credential is refused too, and repeated
    failures lock the token out, so each Cloudflare call consults this first.
    """

    refused = _refused_credentials()
    if prefix in refused:
        raise ProviderError(
            f"Cloudflare refused the request: {refused[prefix]} "
            "Not retried for the rest of this sweep.",
            refusal=CREDENTIAL_REFUSAL,
            reason=refused[prefix],
        )


def _cloudflare_verification(provider: str, connection_ref: str) -> dict[str, Any]:
    """``/user/tokens/verify``'s result for one credential, once per sweep.

    ``{}`` when it does not verify. Called directly rather than through
    ``_cloudflare_envelope``, whose refusals consult this.
    """

    def verify() -> dict[str, Any]:
        url = f"{_cloudflare_url(connection_ref, provider=provider)}/user/tokens/verify"
        headers = {
            "Authorization": (
                f"Bearer {_cloudflare_token(connection_ref, provider=provider)}"
            ),
            "Accept": "application/json",
        }
        try:
            with provider_http._open(url, headers=headers) as response:
                parsed = json.loads(response.read() or b"{}")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            provider_http._release(exc)
            return {}
        result = parsed.get("result") if isinstance(parsed, dict) else None
        if not parsed.get("success") or not isinstance(result, dict):
            return {}
        return result

    prefix = connection_env.connection_prefix(provider, connection_ref)
    return provider_http._snapshot_value(("cloudflare-verification", prefix), verify)


def _cloudflare_verified(provider: str, connection_ref: str) -> bool:
    """Whether the credential itself verifies as active."""

    return _cloudflare_verification(provider, connection_ref).get("status") == "active"


def _cloudflare_refused(
    prefix: str,
    message: str,
    detail: str,
    *,
    status: int = 0,
    verified: Callable[[], bool] | None = None,
) -> ProviderError:
    """The error for one Cloudflare refusal, recording a refused credential."""

    refusal = cloudflare_refusal(detail, status=status, verified=verified)
    if refusal == CREDENTIAL_REFUSAL:
        _refused_credentials()[prefix] = detail
        return ProviderError(message, refusal=CREDENTIAL_REFUSAL, reason=detail)
    return ProviderError(message, refusal=refusal)


def _cloudflare_request(path: str, *, method: str = "GET", payload: Any = None) -> Any:
    """The zone-scoped DNS surface, unwrapped to the result callers expect."""

    return _cloudflare_envelope(path, method=method, payload=payload).get("result")


def _cloudflare_errors(raw: bytes) -> str:
    try:
        parsed = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return "an unreadable error"
    messages = [
        str(error.get("message", "")).strip()
        for error in parsed.get("errors") or ()
        if str(error.get("message", "")).strip()
    ]
    return "; ".join(messages) or "no reason given"


def _cloudflare_paged(path: str) -> list[dict[str, Any]]:
    """Every page of a list endpoint.

    Cloudflare returns 100 records at most. A zone that outgrew one page would
    otherwise have its tail silently reported as absent, and "absent" is the
    word this system acts on, so the reconciler would set about recreating
    records that were there all along.
    """

    collected: list[dict[str, Any]] = []
    page = 1
    while True:
        separator = "&" if "?" in path else "?"
        result = _cloudflare_request(f"{path}{separator}per_page=100&page={page}")
        batch = result or []
        collected.extend(batch)
        if len(batch) < 100:
            return collected
        page += 1
        if page > 50:
            raise ProviderError("Cloudflare list did not terminate.")


def _cloudflare_zones() -> list[dict[str, Any]]:
    return provider_http._snapshot_value(("cloudflare-zones",), lambda: _cloudflare_paged("/zones"))


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
    return _cloudflare_paged(f"/zones/{zone_id}/dns_records")


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
            live = _cloudflare_request(
                f"/zones/{zone_id}/dns_records", method="POST", payload=desired
            )
        return ProviderResult(
            changed=True,
            status=_record_status(zone, live or {}),
            conditions=[
                provider_http._condition("Ready", True, "Created", "DNS record was created.")
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
                provider_http._condition("Ready", True, "Reconciled", "DNS record is current.")
            ],
            message="Public DNS record unchanged.",
        )
    if apply:
        live = _cloudflare_request(
            f"/zones/{zone_id}/dns_records/{live['id']}",
            method="PUT",
            payload=desired,
        )
    return ProviderResult(
        changed=True,
        status=_record_status(zone, live or {}),
        conditions=[provider_http._condition("Ready", True, "Reconciled", "DNS record was updated.")],
        message="Public DNS record updated.",
    )


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
                provider_http._condition("Ready", True, "Absent", "No such record in Cloudflare.")
            ],
            message="Public DNS record was already absent.",
        )
    if apply:
        _cloudflare_request(
            f"/zones/{zone_id}/dns_records/{live['id']}", method="DELETE"
        )
    return ProviderResult(
        changed=True,
        status={"zone": zone, "name": spec.get("name", ""), "removed": True},
        conditions=[provider_http._condition("Ready", True, "Removed", "DNS record was removed.")],
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
        account = cloudflare_analytics._analytics_account()
        domains = _cloudflare_api_cursor_list(
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

    One request per setting: the batch settings endpoint reaches end of life on
    2027-03-31. A failure here is not a failed sweep: the zone still reports its
    records, and a setting refused is the zone's refused "posture" part.
    """

    if not zone_id:
        return {}
    found: dict[str, str] = {}
    for setting in ZONE_POSTURE_SETTINGS:
        try:
            envelope = _cloudflare_api_request(f"/zones/{zone_id}/settings/{setting}")
        except (ProviderError, OSError, ValueError) as exc:
            refuse_part("posture", exc, scope=zone)
            return {}
        item = (envelope or {}).get("result")
        if isinstance(item, dict) and item.get("value") not in (None, ""):
            found[setting] = str(item["value"])
    return found


def list_cloudflare_zones() -> list[dict[str, Any]]:
    """Every zone the credential can see, declared or not.

    Reported in full deliberately: which of them HQ should manage is an
    operator's decision, and it cannot be made on a screen that only lists the
    ones already decided about.
    """

    connection_ref = provider_http._required(connection_env.connection_prefix("cloudflare_dns"), "CONNECTION_REF")
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


# Account readings through `cloudflare_api`. Every list is fetched once per
# sweep through the provider snapshot; per-item requests are made only where
# Cloudflare has no list that carries the field. The account allows 1,200
# requests per five minutes.


def _cloudflare_api_refs() -> tuple[str, ...]:
    # No declared connection still reads once, so a missing credential raises.
    return connection_env.provider_connection_refs("cloudflare_api") or ("",)


def _cloudflare_account(connection_ref: str) -> str:
    return provider_http._snapshot_value(
        ("cloudflare-account", connection_ref),
        lambda: cloudflare_analytics._analytics_account(connection_ref),
    )


def _cloudflare_account_list(
    connection_ref: str, path: str, *, per_page: int = 100
) -> list[dict[str, Any]]:
    """One account list endpoint, read once per sweep."""

    account = _cloudflare_account(connection_ref)
    return provider_http._snapshot_value(
        ("cloudflare-account-list", connection_ref, path),
        lambda: _cloudflare_api_list(
            f"/accounts/{account}{path}", connection_ref, per_page=per_page
        ),
    )


def _cloudflare_api_result(path: str, connection_ref: str) -> Any:
    return (_cloudflare_api_request(path, connection_ref) or {}).get("result")


@reads("cloudflare.pages_project")
def list_pages_projects() -> list[dict[str, Any]]:
    """Pages projects and their latest production deployment."""

    projects = []
    for ref in _cloudflare_api_refs():
        account = _cloudflare_account(ref)
        for project in _cloudflare_account_list(ref, "/pages/projects", per_page=10):
            deployment = project.get("canonical_deployment") or {}
            metadata = (deployment.get("deployment_trigger") or {}).get("metadata") or {}
            projects.append(
                {
                    "connection_ref": ref,
                    "account_id": account,
                    "name": project.get("name", ""),
                    "subdomain": project.get("subdomain") or "",
                    "domains": tuple(project.get("domains") or ()),
                    "production_branch": project.get("production_branch") or "",
                    "deployment_id": deployment.get("id") or "",
                    "deployment_commit": str(metadata.get("commit_hash") or "")[:7],
                    "deployment_created_on": deployment.get("created_on") or "",
                }
            )
    return projects


@reads("cloudflare.d1_database")
def list_d1_databases() -> list[dict[str, Any]]:
    """D1 databases. The list omits file_size, so each is read once more."""

    databases = []
    for ref in _cloudflare_api_refs():
        account = _cloudflare_account(ref)
        for database in _cloudflare_account_list(ref, "/d1/database"):
            uuid = str(database.get("uuid", ""))
            record = {
                "connection_ref": ref,
                "account_id": account,
                "name": database.get("name", ""),
                "uuid": uuid,
                "created_at": database.get("created_at") or "",
                "version": database.get("version") or "",
            }
            try:
                detail = _cloudflare_api_result(
                    f"/accounts/{account}/d1/database/{uuid}", ref
                )
            except (ProviderError, OSError, ValueError) as exc:
                refuse_part(
                    "file_size", exc, scope=str(record["name"]), connection_ref=ref
                )
            else:
                size = (detail or {}).get("file_size")
                if isinstance(size, int):
                    record["file_size"] = size
            databases.append(record)
    return databases


def _access_destination_hosts(destinations: Any) -> tuple[str, ...]:
    """Hostnames from an application's destinations, without paths or CIDRs."""

    hosts: list[str] = []
    for destination in destinations or ():
        if not isinstance(destination, dict):
            continue
        if destination.get("type") == "public":
            uri = str(destination.get("uri") or "")
            host = uri.split("://", 1)[-1].split("/", 1)[0]
        else:
            host = str(destination.get("hostname") or "")
        host = host.strip().lower().rstrip(".")
        if host and host not in hosts:
            hosts.append(host)
    return tuple(hosts)


@reads("cloudflare.access_app")
def list_access_apps() -> list[dict[str, Any]]:
    """Access applications at the account, with their policies by name."""

    apps = []
    for ref in _cloudflare_api_refs():
        account = _cloudflare_account(ref)
        for app in _cloudflare_account_list(ref, "/access/apps"):
            apps.append(
                {
                    "connection_ref": ref,
                    "account_id": account,
                    "id": app.get("id", ""),
                    "name": app.get("name") or "",
                    "type": app.get("type") or "",
                    "domain": app.get("domain") or "",
                    "destinations": _access_destination_hosts(app.get("destinations")),
                    "session_duration": app.get("session_duration") or "",
                    "policies": tuple(
                        {"id": policy.get("id") or "", "name": policy.get("name") or ""}
                        for policy in app.get("policies") or ()
                        if isinstance(policy, dict)
                    ),
                }
            )
    return apps


def _admits_token(rule: Any, token_id: str) -> bool:
    if not isinstance(rule, dict):
        return False
    if "any_valid_service_token" in rule:
        return True
    return str((rule.get("service_token") or {}).get("token_id") or "") == token_id


def _apps_admitting(apps: list[dict[str, Any]], token_id: str) -> tuple[dict, ...]:
    return tuple(
        {"id": app.get("id") or "", "name": app.get("name") or ""}
        for app in apps
        if any(
            _admits_token(rule, token_id)
            for policy in app.get("policies") or ()
            if isinstance(policy, dict)
            for rule in policy.get("include") or ()
        )
    )


@reads("cloudflare.access_service_token")
def list_access_service_tokens() -> list[dict[str, Any]]:
    """Service tokens, and the applications whose policies include them.

    The client ID is never read into a record.
    """

    tokens = []
    for ref in _cloudflare_api_refs():
        listed = _cloudflare_account_list(ref, "/access/service_tokens")
        try:
            apps = _cloudflare_account_list(ref, "/access/apps")
            unread = False
        except (ProviderError, OSError, ValueError) as exc:
            refuse_part("apps", exc, connection_ref=ref)
            apps, unread = [], True
        for token in listed:
            token_id = str(token.get("id", ""))
            record = {
                "connection_ref": ref,
                "id": token_id,
                "name": token.get("name") or "",
                "expires_at": token.get("expires_at") or "",
                "created_at": token.get("created_at") or "",
            }
            if not unread:
                record["apps"] = _apps_admitting(apps, token_id)
            tokens.append(record)
    return tokens


def _tunnel_ingress(config: Any) -> tuple[dict[str, str], ...]:
    rules = ((config or {}).get("config") or {}).get("ingress") or ()
    return tuple(
        {"hostname": str(rule["hostname"]), "service": str(rule.get("service") or "")}
        for rule in rules
        if isinstance(rule, dict) and rule.get("hostname")
    )


def _tunnel_connections(clients: Any) -> tuple[dict[str, str], ...]:
    found = []
    for client in clients or ():
        if not isinstance(client, dict):
            continue
        for connection in client.get("conns") or ():
            if not isinstance(connection, dict):
                continue
            found.append(
                {
                    "version": str(
                        client.get("version") or connection.get("client_version") or ""
                    ),
                    "colo": str(connection.get("colo_name") or ""),
                    "origin_ip": str(connection.get("origin_ip") or ""),
                }
            )
    return tuple(found)


@reads("cloudflare.tunnel")
def list_tunnels() -> list[dict[str, Any]]:
    """Tunnels, their ingress and their live connections.

    Connections come from each tunnel's own endpoint: the list's ``connections``
    field is removed on 2026-10-05 and is not read.
    """

    tunnels = []
    for ref in _cloudflare_api_refs():
        account = _cloudflare_account(ref)
        for tunnel in _cloudflare_account_list(ref, "/cfd_tunnel?is_deleted=false"):
            tunnel_id = str(tunnel.get("id", ""))
            base = f"/accounts/{account}/cfd_tunnel/{tunnel_id}"
            record: dict[str, Any] = {
                "connection_ref": ref,
                "account_id": account,
                "id": tunnel_id,
                "name": tunnel.get("name") or "",
                "status": tunnel.get("status") or "",
                "created_at": tunnel.get("created_at") or "",
                "conns_active_at": tunnel.get("conns_active_at") or "",
            }
            scope = {"scope": record["name"], "connection_ref": ref}
            try:
                config = _cloudflare_api_result(f"{base}/configurations", ref)
            except (ProviderError, OSError, ValueError) as exc:
                refuse_part("configuration", exc, **scope)
            else:
                record["config_source"] = str((config or {}).get("source") or "")
                record["ingress"] = _tunnel_ingress(config)
            try:
                clients = _cloudflare_api_result(f"{base}/connections", ref)
            except (ProviderError, OSError, ValueError) as exc:
                refuse_part("connections", exc, **scope)
            else:
                record["connections"] = _tunnel_connections(clients)
            tunnels.append(record)
    return tunnels


def _cloudflare_api_zones(connection_ref: str) -> list[dict[str, Any]]:
    return provider_http._snapshot_value(
        ("cloudflare-api-zones", connection_ref),
        lambda: _cloudflare_api_list("/zones", connection_ref, per_page=50),
    )


def _earliest_expiry(pack: dict[str, Any]) -> str:
    dates = sorted(
        str(certificate.get("expires_on"))
        for certificate in pack.get("certificates") or ()
        if isinstance(certificate, dict) and certificate.get("expires_on")
    )
    return dates[0] if dates else ""


@reads("cloudflare.edge_certificate")
def list_edge_certificates() -> list[dict[str, Any]]:
    """Certificate packs on every zone the account credential can see.

    A zone whose packs are refused is a refused part on that zone; every zone
    refused is a refused read and raises.
    """

    packs: list[dict[str, Any]] = []
    for ref in _cloudflare_api_refs():
        zones = [zone for zone in _cloudflare_api_zones(ref) if zone.get("name")]
        refused: list[ProviderError] = []
        for zone in zones:
            name = str(zone["name"]).strip().lower().rstrip(".")
            account = str((zone.get("account") or {}).get("id") or "")
            try:
                listed = _cloudflare_api_list(
                    f"/zones/{zone.get('id', '')}/ssl/certificate_packs?status=all",
                    ref,
                    per_page=50,
                )
            except (ProviderError, OSError, ValueError) as exc:
                refused.append(
                    ProviderError(
                        _unread_reason(exc),
                        refusal=getattr(exc, "refusal", ""),
                        reason=getattr(exc, "reason", ""),
                    )
                )
                refuse_part("", exc, scope=name, connection_ref=ref)
                continue
            packs.extend(
                {
                    "connection_ref": ref,
                    "account_id": account,
                    "zone": name,
                    "id": pack.get("id") or "",
                    "type": pack.get("type") or "",
                    "hosts": tuple(pack.get("hosts") or ()),
                    "status": pack.get("status") or "",
                    "certificate_authority": pack.get("certificate_authority") or "",
                    "expires_on": _earliest_expiry(pack),
                }
                for pack in listed
            )
        if zones and len(refused) == len(zones):
            raise refused[0]
    return packs


@reads("cloudflare.redirect")
def list_redirects() -> list[dict[str, Any]]:
    """Redirect rules and forwarding page rules on every zone the credential sees."""

    return redirects.read(
        _cloudflare_api_refs(),
        redirects.ZoneReads(
            zones=_cloudflare_api_zones,
            listed=lambda path, ref: _cloudflare_api_list(path, ref, per_page=50),
            result=_cloudflare_api_result,
            reason=_unread_reason,
            error=ProviderError,
            refuse=refuse_part,
        ),
    )


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


def _probe_cloudflare_dns(connection_ref: str) -> dict[str, Any]:
    verification = _cloudflare_envelope(
        "/user/tokens/verify", connection_ref=connection_ref
    )
    # Which zones *matter* is not the controller's to know. The credential
    # reports what it can reach; HQ declares which zones it is responsible for
    # and is the only side able to compare the two.
    zones = _cloudflare_envelope(
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

    verification = _cloudflare_api_request("/user/tokens/verify", connection_ref)
    if not isinstance(verification, dict) or not verification.get("success"):
        raise ProviderError("Cloudflare token verification failed.")

    account = cloudflare_analytics._analytics_account(connection_ref)
    hosts = sorted(site["host"] for site in cloudflare_analytics._analytics_sites(account, connection_ref))
    measured = "site" if len(hosts) == 1 else "sites"
    return {
        "detail": f"{len(hosts)} analytics {measured}.",
        "reaches": hosts,
        "expires_at": _token_expiry(verification),
    }


def _cloudflare_api_request(path: str, connection_ref: str = "") -> Any:
    """One account-surface request through the account-scoped credential."""

    return _cloudflare_envelope(
        path, provider="cloudflare_api", connection_ref=connection_ref
    )


def _cloudflare_api_list(
    path: str, connection_ref: str = "", *, per_page: int = 100
) -> list[dict[str, Any]]:
    """Every page from one Cloudflare account list endpoint."""

    collected: list[dict[str, Any]] = []
    for page in range(1, 51):
        separator = "&" if "?" in path else "?"
        response = _cloudflare_api_request(
            f"{path}{separator}per_page={per_page}&page={page}", connection_ref
        )
        batch = (response or {}).get("result", [])
        if not isinstance(batch, list):
            raise ProviderError("Cloudflare account list returned an invalid result.")
        collected.extend(item for item in batch if isinstance(item, dict))
        total_pages = int(
            ((response or {}).get("result_info") or {}).get("total_pages") or 0
        )
        # total_pages decides when present: an endpoint may cap per_page below
        # what was asked, so a short page is not proof of the last one.
        if (page >= total_pages) if total_pages else (len(batch) < per_page):
            return collected
    raise ProviderError("Cloudflare account list did not terminate.")


def _cloudflare_api_cursor_list(
    path: str, connection_ref: str = "", *, per_page: int = 50
) -> list[dict[str, Any]]:
    """Every page from a cursor-paginated Cloudflare list; an empty cursor ends it."""

    collected: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(200):
        query = f"per_page={per_page}" + (f"&cursor={urllib.parse.quote(cursor, safe='')}" if cursor else "")
        separator = "&" if "?" in path else "?"
        response = _cloudflare_api_request(f"{path}{separator}{query}", connection_ref)
        batch = (response or {}).get("result", [])
        if not isinstance(batch, list):
            raise ProviderError("Cloudflare account list returned an invalid result.")
        collected.extend(item for item in batch if isinstance(item, dict))
        cursor = str(((response or {}).get("result_info") or {}).get("cursor") or "")
        if not cursor:
            return collected
    raise ProviderError("Cloudflare account list did not terminate.")
