"""Which connection a reading belongs to."""

from __future__ import annotations

# Providers HQ holds many connections of, each reaching a different machine. A
# record read through one names that connection, so no two connections can
# share a reading. Refused here when a kind of one does not.
PER_CONNECTION_PROVIDERS = frozenset({"ssh"})


def unattributed_kinds(observations, providers) -> list[str]:
    """Kinds of a per-connection provider whose records do not name their connection."""

    found = [
        kind
        for kind, spec in observations.items()
        if spec.provider in PER_CONNECTION_PROVIDERS
        and not (
            "connection_ref" in spec.record.model_fields
            and spec.record.model_fields["connection_ref"].is_required()
        )
    ]
    for spec in providers:
        if spec.unobserved_reason or spec.from_record is None:
            continue
        if not set(spec.connection_providers) & PER_CONNECTION_PROVIDERS:
            continue
        sample = dict(spec.sample_record or {})
        if not sample.get("connection_ref") or "connection_ref" not in spec.from_record(sample):
            found.append(spec.kind)
    return sorted(found)

