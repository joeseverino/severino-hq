"""Which connection a reading belongs to."""

from .connection_kinds import LOGS_IN


def unattributed_kinds(observations, providers) -> list[str]:
    """Kinds read through a connection that logs in to one machine whose
    records do not name their connection: HQ holds one per machine, so no two
    may share a reading."""

    found = [
        kind
        for kind, spec in observations.items()
        if spec.provider in LOGS_IN
        and not (
            "connection_ref" in spec.record.model_fields and spec.record.model_fields["connection_ref"].is_required()
        )
    ]
    for spec in providers:
        if not spec.sweeps:
            continue
        if not set(spec.connection_providers) & LOGS_IN:
            continue
        sample = dict(spec.sample_record or {})
        if not sample.get("connection_ref") or "connection_ref" not in (spec.record_spec(sample) or {}):
            found.append(spec.kind)
    return sorted(found)
