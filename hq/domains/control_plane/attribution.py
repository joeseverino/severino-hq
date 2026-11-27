"""Which kinds are read by logging in, and which connection such a reading belongs to."""

from .connection_kinds import LOGS_IN


def logging_in(observations, providers) -> frozenset[str]:
    """The kinds read by logging in to one machine, the only ones whose reader
    the controller lets open a shell."""

    through = {spec.kind: {*spec.connection_providers} for spec in providers}
    through |= {kind: {spec.provider} for kind, spec in observations.items()}
    return frozenset(kind for kind, via in through.items() if via & LOGS_IN)


def unattributed_kinds(observations, providers) -> list[str]:
    """Kinds read by logging in whose records do not name their connection:
    HQ holds one per machine, so no two may share a reading."""

    providers = tuple(providers)
    logs_in = logging_in(observations, providers)
    found = [
        kind
        for kind, spec in observations.items()
        if kind in logs_in
        and not (
            "connection_ref" in spec.record.model_fields and spec.record.model_fields["connection_ref"].is_required()
        )
    ]
    for spec in providers:
        if not spec.sweeps or spec.kind not in logs_in:
            continue
        sample = dict(spec.sample_record or {})
        if not sample.get("connection_ref") or "connection_ref" not in (spec.record_spec(sample) or {}):
            found.append(spec.kind)
    return sorted(found)
