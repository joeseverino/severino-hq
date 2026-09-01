"""AdGuard emits its declaration and every controller surface together."""

from __future__ import annotations

import base64
from typing import Any

from pydantic import Field

from .contracts import (
    ControllerProviderAdapter,
    ProviderError,
    ProviderResult,
    ProviderRuntime,
)


def _url(runtime: ProviderRuntime, connection_ref: str = "") -> str:
    prefix = runtime.connection_prefix("adguard", connection_ref)
    return runtime.required(prefix, "URL").rstrip("/")


def _headers(runtime: ProviderRuntime, connection_ref: str = "") -> dict[str, str]:
    prefix = runtime.connection_prefix("adguard", connection_ref)
    encoded = base64.b64encode(
        f"{runtime.required(prefix, 'USERNAME')}:{runtime.required(prefix, 'PASSWORD')}".encode()
    ).decode()
    return {"Authorization": f"Basic {encoded}"}


def reconcile(
    runtime: ProviderRuntime,
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    base_url = _url(runtime)
    headers = _headers(runtime)
    rewrites = runtime.request(f"{base_url}/control/rewrite/list", headers=headers)
    desired = {"domain": spec["domain"], "answer": spec["answer"]}
    matches = [item for item in rewrites if item.get("domain") == spec["domain"]]
    if not matches:
        previous = (observed or {}).get("domain")
        if previous and previous != spec["domain"]:
            matches = [item for item in rewrites if item.get("domain") == previous]
    if len(matches) > 1:
        raise ProviderError("AdGuard contains duplicate rewrites for the domain.")
    if len(matches) == 1 and all(
        matches[0].get(key) == value for key, value in desired.items()
    ):
        changed = False
    elif matches:
        if apply:
            runtime.request(
                f"{base_url}/control/rewrite/update",
                method="PUT",
                headers=headers,
                payload={
                    "target": {
                        "domain": matches[0]["domain"],
                        "answer": matches[0]["answer"],
                    },
                    "update": desired,
                },
            )
        changed = True
    else:
        if apply:
            runtime.request(
                f"{base_url}/control/rewrite/add",
                method="POST",
                headers=headers,
                payload=desired,
            )
        changed = True

    live = matches[0] if matches else {}
    status = {**desired, "enabled": live.get("enabled", True)}
    if live.get("enabled") is False:
        return ProviderResult(
            changed=changed,
            status=status,
            conditions=[
                runtime.condition(
                    "Degraded",
                    True,
                    "Disabled",
                    "The rewrite exists in AdGuard but is switched off, so the "
                    "name does not resolve. Re-enable it in AdGuard.",
                )
            ],
            message="AdGuard rewrite is present but disabled.",
        )
    return ProviderResult(
        changed=changed,
        status=status,
        conditions=[
            runtime.condition(
                "Ready", True, "Reconciled", "AdGuard rewrite is current."
            )
        ],
        message="AdGuard rewrite updated." if changed else "AdGuard rewrite unchanged.",
    )


def delete(
    runtime: ProviderRuntime,
    spec: dict[str, Any],
    *,
    apply: bool = True,
    observed: dict[str, Any] | None = None,
) -> ProviderResult:
    del observed
    base_url = _url(runtime)
    headers = _headers(runtime)
    rewrites = runtime.request(f"{base_url}/control/rewrite/list", headers=headers)
    matches = [item for item in rewrites if item.get("domain") == spec["domain"]]
    if not matches:
        return ProviderResult(
            changed=False,
            status={"domain": spec["domain"], "removed": True},
            conditions=[
                runtime.condition(
                    "Ready", True, "Absent", "No such rewrite in AdGuard."
                )
            ],
            message="AdGuard rewrite was already absent.",
        )
    if apply:
        for match in matches:
            runtime.request(
                f"{base_url}/control/rewrite/delete",
                method="POST",
                headers=headers,
                payload={"domain": match["domain"], "answer": match["answer"]},
            )
    return ProviderResult(
        changed=True,
        status={"domain": spec["domain"], "removed": True},
        conditions=[
            runtime.condition(
                "Ready", True, "Removed", "AdGuard rewrite was removed."
            )
        ],
        message="AdGuard rewrite removed.",
    )


def inventory(runtime: ProviderRuntime) -> list[dict[str, Any]]:
    base_url = _url(runtime)
    records = runtime.request(
        f"{base_url}/control/rewrite/list", headers=_headers(runtime)
    )
    return [
        {
            "domain": item["domain"],
            "answer": item["answer"],
            "enabled": item.get("enabled", True),
        }
        for item in records
        if item.get("domain") and item.get("answer")
    ]


def probe(runtime: ProviderRuntime, connection_ref: str) -> dict[str, Any]:
    status = runtime.request(
        f"{_url(runtime, connection_ref)}/control/status",
        headers=_headers(runtime, connection_ref),
    )
    if not isinstance(status, dict) or "dns_addresses" not in status:
        raise ProviderError("AdGuard did not return a status.")
    return {"detail": f"AdGuard {status.get('version', '')}".strip(), "reaches": []}


def _answers(spec: dict[str, Any]) -> tuple[str, ...]:
    answer = str(spec.get("answer", "")).strip()
    return (answer,) if answer else ()


def _hostnames(spec: dict[str, Any]) -> tuple[str, ...]:
    return (spec["domain"],)


def _readout(
    spec: dict[str, Any], status: dict[str, Any]
) -> tuple[tuple[str, str, str], ...]:
    return (("Answers with", spec.get("answer", ""), status.get("answer", "")),)


def _origin(spec: dict[str, Any]) -> str:
    answers = _answers(spec)
    return answers[0] if answers else ""


def _from_record(record: dict[str, Any]) -> dict[str, Any]:
    return {"domain": record["domain"], "answer": record["answer"]}


def _seed(context: Any) -> dict[str, Any]:
    return {"domain": context.hostname}


def build_adapter(*, provider_model, provider_spec, applies):
    """Build after the host's provider primitives exist; no parent import cycle."""

    class AdGuardRewriteSpec(provider_model):
        domain: str = Field(
            min_length=1,
            max_length=253,
            title="Hostname",
            description="The name that should resolve on your network.",
        )
        answer: str = Field(
            min_length=1,
            max_length=253,
            title="Points at",
            description="The address this hostname resolves to, usually an IP.",
        )

    return ControllerProviderAdapter(
        definition=provider_spec(
            "adguard.rewrite",
            "Makes a hostname resolve to an IP on your network. Created in AdGuard "
            "if it is not there yet.",
            AdGuardRewriteSpec,
            actions={"reconcile": applies(automatic=True), "delete": applies()},
            label="Internal DNS record",
            connection_providers=("adguard",),
            removal_note=lambda spec: (
                f"{spec.get('domain', 'This name')} stops resolving on the LAN, so "
                "anything reached by that name goes dark inside the network."
            ),
            facet="dns",
            hostnames=_hostnames,
            seed=_seed,
            answers=_answers,
            origin=_origin,
            from_record=_from_record,
            sample_record={"domain": "app.example.com", "answer": "10.0.0.10"},
            readout=_readout,
        ),
        inventory=inventory,
        connection_probes={"adguard": probe},
        actions={"reconcile": reconcile, "delete": delete},
    )
