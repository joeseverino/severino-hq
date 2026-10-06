"""AdGuard: the internal DNS rewrites HQ declares. The controller reads and writes them."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ..connection_shapes import LOGIN
from ..provider_spec import ConnectionKind, ProviderModel, ProviderSpec, applies


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


class AdGuardRewriteSpec(ProviderModel):
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
        description="The IP address this hostname resolves to.",
    )

DEFINITION = ProviderSpec(
    "adguard.rewrite",
    "Resolves a hostname to an IP on your network. HQ creates it in "
    "AdGuard if it does not exist.",
    AdGuardRewriteSpec,
    actions={"reconcile": applies(automatic=True), "delete": applies()},
    label="Internal DNS record",
    connection_providers=("adguard",),
    removal_note=lambda spec: (
        f"{spec.get('domain', 'This name')} stops resolving on your "
        "network. Anything reached by that name goes offline."
    ),
    facet="dns",
    hostnames=_hostnames,
    seed=_seed,
    answers=_answers,
    origin=_origin,
    from_record=_from_record,
    sample_record={"domain": "app.example.com", "answer": "10.0.0.10"},
    readout=_readout,
)
DEFINITIONS = (DEFINITION,)

# The connection this provider's credential arrives through, beside its kinds:
# admitting the module admits both.
CONNECTIONS = {"adguard": ConnectionKind("AdGuard Home", "coarse", LOGIN)}
