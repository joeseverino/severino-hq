"""What the providers hold, and which of it HQ does not manage.

The controller fetches every record a provider holds on each pass. This records
what HQ does not manage. It is a cache and stays one: nothing reconciles from it,
and HQ never becomes a second copy of AdGuard. What it buys is the difference
between a registry and a console: an operator can see what exists, and adopt
what HQ should be looking after.

Adoption is safe because the spec is read back out of the live record through
the provider's own ``from_record``. The declaration starts equal to the world,
so the first reconciliation after adopting changes nothing. Anything else would
mean adopting a host quietly reset it to HQ's defaults.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from django.db import transaction
from django.utils import timezone

from hq.domains.control_plane.models import (
    ManagedResource,
    ProviderConnection,
    ProviderInventory,
)
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.reading_parts import clean_refused_parts, refused_parts
from hq.domains.control_plane.providers import OBSERVATION_KINDS, PROVIDERS, registry_label
from hq.platform.core.audit import CONNECTION_AUDIT_TYPE, record_event
from hq.platform.core.models import AuditLog

from hq.domains.control_plane.names import normalized_hostname

from .conditions import stamped
from .contracts import endpoint_has_private_parts
from .credential_mint import parse_expiry, store_references
from .security import Capability, Principal
from .ui import counted


def record_token(kind: str, identity: tuple[str, ...]) -> str:
    """A short, stable handle for one live record, safe to put in a URL.

    Derived rather than stored because nothing persists an unmanaged record,
    it exists only in the last sweep. Hashed rather than joined because an
    identity contains a DNS value, and a TXT record's value is neither short nor
    URL-safe.

    Shared with whatever else needs to name the same record, so a page offering
    to adopt something computes the same handle the adoption looks it up by.
    """

    return hashlib.sha256("\x1f".join((kind, *identity)).encode()).hexdigest()[:16]


@transaction.atomic
def record_inventory(
    payload: dict[str, Any], *, principal: Principal, controller_id: str = ""
) -> dict[str, Any]:
    """Store one controller sweep, replacing whatever the last one said.

    Replaced rather than merged: this describes a provider at a moment, and
    merging would keep records that have since been deleted, which is the one
    thing a staleness-aware cache must not do.

    ``payload`` is the contract's ``Inventory``, which the bridge holds every
    sweep to before this runs; a member is read as the type the contract gives it.

    A provider that could not be reached is the exception, and the reason is
    the same one. "The credential is missing" and "the provider is empty" are
    different facts, and a sweep that reports the first must not be stored as
    the second: doing so deletes what HQ knew about a host that never changed,
    and every surface downstream then says the containers are gone. So a failed
    report keeps the last records and the moment they were seen, and records
    only that the provider could not be confirmed. The data ages visibly
    instead of vanishing silently, which is what ``observed_at`` is for.
    """

    principal.require(Capability.MANAGE_INFRASTRUCTURE)
    observed_at = timezone.now()
    stored = []
    summary: dict[str, dict[str, Any]] = {}
    for kind, report in sorted(payload.items()):
        if kind not in PROVIDERS and kind not in OBSERVATION_KINDS:
            # A controller ahead of this HQ. Ignored rather than rejected: the
            # rest of the sweep is still true, and refusing it would make a
            # controller upgrade take the whole inventory down.
            continue
        if report.get("carried"):
            # A kind on its own clock that was not due: nothing was asked of
            # the provider, so what HQ holds is untouched. A refusal reported
            # with it (the provider's allowance is spent) is an attempt, and
            # is stored as one beside the records it could not replace.
            parts = clean_refused_parts(kind, report.get("refused_parts"))
            if parts:
                ProviderInventory.objects.filter(kind=kind).update(
                    refused_parts=parts, controller_id=controller_id, updated_at=observed_at
                )
                stored.append(kind)
            continue
        reached = report["ok"]
        connected = report.get("connected", True)
        records = report["records"]
        error = report.get("error", "")
        refusal = report.get("refusal", "") if not reached else ""
        observation = OBSERVATIONS.get(kind)
        if observation is not None:
            records, refused = observation.clean(records)
            if refused:
                error = error or (
                    f"{counted(refused, 'record')} did not match the {kind} schema."
                )
        seen = {"records": records, "observed_at": observed_at}
        # A refused read refuses every part; only a read that answered has some.
        parts = clean_refused_parts(kind, report.get("refused_parts")) if reached else []
        # The last records, for a kind whose changes are worth a moment on the
        # history (see ``_record_change``); other kinds pay no query for it.
        logs_changes = reached and getattr(PROVIDERS.get(kind), "from_record", None) is not None
        before = (
            ProviderInventory.objects.filter(kind=kind).values_list("records", flat=True).first()
            if logs_changes
            else None
        )
        row, _ = ProviderInventory.objects.update_or_create(
            kind=kind,
            # An unreachable provider leaves the last sweep's records and the
            # moment it took them exactly where they were.
            defaults={
                "reachable": reached,
                "connected": connected,
                "error": error[:500],
                "refusal": refusal,
                "refused_parts": parts,
                "controller_id": controller_id,
                **(seen if reached else {}),
            },
            create_defaults={
                "reachable": reached,
                "connected": connected,
                "error": error[:500],
                "refusal": refusal,
                "refused_parts": parts,
                "controller_id": controller_id,
                **seen,
            },
        )
        stored.append(kind)
        summary[kind] = _summary(row)
        if before is not None:
            _record_change(row, before, records, controller_id)

    # Adoption is not done here. A record in a domain HQ has been made
    # responsible for is HQ's, but which records those are is `zones`' to say,
    # and reaching for it from inside the sweep would make the two modules
    # import each other. `application.sweep` composes the pair instead.
    return {
        "ok": True,
        "recorded": stored,
        "observed_at": observed_at.isoformat(),
        "kinds": summary,
    }


READING_AUDIT_TYPE = "Reading"


def _record_change(row: ProviderInventory, before: list[Any], after: list[Any], controller_id: str) -> None:
    """A reading's records changed between two sweeps: the moment, for the history.

    Readings keep only their latest records, so without this a DNS record or a
    policy edited outside HQ changes nothing anyone can point at in time.

    Compared as what a declaration of each record would hold (the kind's
    ``from_record``), so a container's "Up 3 days" is not a change and its image
    is. A kind with no such shape is not logged. Counts only, never the records:
    a policy document in the log is a second copy of it.
    """

    provider = PROVIDERS.get(row.kind)
    if provider is None or provider.from_record is None:
        return

    def canonical(records: list[Any]) -> set[str]:
        found = set()
        for record in records or ():
            spec = _spec_from_record(row.kind, record)
            if spec is not None:
                found.add(json.dumps(spec, sort_keys=True, default=str))
        return found

    old, new = canonical(before), canonical(after)
    if old == new:
        return
    added, gone = len(new - old), len(old - new)
    parts = [counted(added, "record new or changed", "records new or changed")] if added else []
    parts += [counted(gone, "record gone", "records gone")] if gone else []
    record_event(
        action=AuditLog.Action.OBSERVED,
        obj=row,
        type_label=READING_AUDIT_TYPE,
        message=f"{registry_label(row.kind)} changed: {', '.join(parts)}",
        metadata={"kind": row.kind, "controller_id": controller_id, "new": added, "gone": gone},
    )


def _summary(row: ProviderInventory) -> dict[str, Any]:
    """One kind as the sweep's own summary says it: its state in credential
    sight's words, its record count, and each part refused."""

    from .credential_sight import standing

    state, label = standing(row)
    return {
        "state": state,
        "label": label,
        "records": len(row.records or ()) if row.connected and row.reachable else None,
        "refused_parts": [refused.phrase for refused in refused_parts(row)],
    }


def confirm_observed(payload: dict[str, Any]) -> int:
    """Mark declarations the sweep just found still matching as observed.

    A declaration is "in sync" when what HQ asked for is what is there, and a
    sweep is HQ going and looking. Nothing queues a reconcile for a resource
    that has not drifted, so without this an unchanged declaration would never
    be recorded as observed.

    Only where the spec still matches the live record. A declaration that has
    drifted is exactly the one a reconcile should visit, and quietly calling it
    observed would hide the difference this whole model exists to surface.

    A declaration whose observation changed is saved, so the change is audited
    and indexed. The rest, found exactly as they were last observed, take the
    moment in one statement for the whole sweep: no save, no event and nothing
    to index.
    """

    seen = timezone.now()
    confirmed = 0
    unchanged: list[Any] = []
    for kind, report in payload.items():
        if kind not in PROVIDERS or not report["ok"]:
            continue
        live = _live_specs(kind, report["records"])
        if not live:
            continue
        for resource in ManagedResource.objects.filter(kind=kind, enabled=True):
            found = live.get(record_identity(kind, resource.spec))
            if found is None:
                continue
            drift = _differences(kind, resource.spec, found)
            if drift:
                _record_drift(resource, drift)
                continue
            if not _observe(resource, found, seen):
                unchanged.append(resource.pk)
            confirmed += 1
    if unchanged:
        ManagedResource.objects.filter(pk__in=unchanged).update(last_observed_at=seen)
    return confirmed


def _live_specs(kind: str, records: list[Any]) -> dict[tuple[str, ...], dict[str, Any]]:
    """Each live record as the spec a declaration of it would hold, by identity."""

    live = {}
    for record in records:
        spec = _spec_from_record(kind, record)
        if spec is not None:
            live[record_identity(kind, spec)] = spec
    return live


def _observe(resource: ManagedResource, found: dict[str, Any], seen: datetime) -> bool:
    """Save what the sweep found when it differs from what is stored; whether it did."""

    status = dict(found)
    conditions = stamped(resource.conditions, [
        {
            "type": "Ready",
            "status": True,
            "reason": "Observed",
            "message": "The last sweep found this exactly as declared.",
        }
    ], seen)
    if (
        resource.observed_generation == resource.generation
        and resource.status == status
        and resource.conditions == conditions
    ):
        return False
    resource.observed_generation = resource.generation
    resource.last_observed_at = seen
    resource.status = status
    resource.conditions = conditions
    resource.save(
        update_fields=[
            "observed_generation",
            "last_observed_at",
            "status",
            "conditions",
        ]
    )
    return True


def retire_departed(payload: dict[str, Any]) -> list[str]:
    """Forget containers a complete listing of their machine no longer holds.

    The listing includes stopped containers, so one missing from it was
    deleted, not stopped: a one-off ``docker run``, or a compose service
    renamed. HQ cannot create a container (its compose file defines it), so a
    declaration of one that is gone can never be met, and keeping it only
    raises a finding nobody can clear. The sweep that adopts what appears
    forgets what departs.

    Only machines this sweep listed: an unreachable one lists nothing and
    retires nothing. A container marked on demand is kept, being declared
    precisely because it comes and goes. Nothing is kept out, so a container
    that returns under the same name is adopted again.
    """

    from hq.domains.control_plane.provider_adapters.portainer import CONTAINER_KIND

    report = payload.get(CONTAINER_KIND)
    if report is None or not report["ok"] or report.get("refused_parts") or not report["records"]:
        return []
    records = report["records"]
    from_record = PROVIDERS[CONTAINER_KIND].from_record
    if from_record is None:
        return []
    listed = {record_identity(CONTAINER_KIND, from_record(record)) for record in records}
    hosts = {identity[0] for identity in listed}
    retired = []
    for resource in ManagedResource.objects.filter(kind=CONTAINER_KIND):
        identity = record_identity(CONTAINER_KIND, resource.spec)
        if identity[0] not in hosts or identity in listed or resource.spec.get("on_demand"):
            continue
        retired.append(resource.key)
        resource.audit_gone = f"Forgot {resource.key}: no longer on its machine"
        resource.delete()
    return retired


def live_spec(kind: str, spec: dict[str, Any]) -> dict[str, Any] | None:
    """What the last sweep found for the record ``spec`` declares, as a spec."""

    from .facts import inventory_records

    identity = record_identity(kind, spec)
    for _snapshot, record in inventory_records(kind):
        found = _spec_from_record(kind, record)
        if found is not None and record_identity(kind, found) == identity:
            return found
    return None


def _spec_from_record(kind: str, record: dict[str, Any]) -> dict[str, Any] | None:
    provider = PROVIDERS[kind]
    if provider.from_record is None:
        return None
    try:
        return provider.from_record(record)
    except (KeyError, TypeError, ValueError):
        return None


def _differences(
    kind: str, declared: dict[str, Any], found: dict[str, Any]
) -> tuple[tuple[str, str, str], ...]:
    """``(field, asked for, found)`` for every field the live record contradicts.

    The comparison rule above, returning what it saw rather than only whether
    it saw anything, so "do these match" and "how do they differ" cannot
    disagree.
    """

    unobservable = PROVIDERS[kind].unobservable_fields
    return tuple(
        (field, str(value), str(found.get(field, "")))
        for field, value in declared.items()
        if field in found
        and field not in unobservable
        and _text(found.get(field, "")) != _text(value)
    )


def _text(value: Any) -> str:
    """One value as a string, with line endings settled.

    A browser submits a textarea as CRLF and every provider returns LF, so a
    multi-line field saved through a form differs from the identical document
    read back: byte for byte the same but for the line endings.
    """

    text = str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n") if "\r" in text else text
    return _canonical_document(text)


def _canonical_document(text: str) -> str:
    """A JSON document reduced to what it says, so layout is not a difference.

    HQ stores a document such as the tailnet policy minified, and the provider
    hands it back pretty-printed. Compared as text they never match, so a
    policy would read as drifted from the moment it was applied.

    Only a value that parses as a JSON object or array is touched; anything else,
    including a policy written as HuJSON with comments, is compared as the text
    it is, which errs towards reporting a difference.
    """

    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return text
    try:
        parsed = json.loads(stripped)
    except ValueError:
        return text
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"))


def _record_drift(
    resource: ManagedResource, drift: tuple[tuple[str, str, str], ...]
) -> None:
    """Say what the sweep found instead, rather than leaving the last good word.

    A declaration the live record contradicts is described, not left with the
    condition from the last time it matched.

    Still not marked observed: this is not what HQ asked for, and the timestamp
    has to keep meaning "last seen as declared" or it means nothing. What gets
    written is the disagreement itself, named field by field, because which side
    is wrong is not HQ's to decide: often it is the declaration that is out of
    date, and an operator can only see that if HQ says which value it is arguing
    about.
    """

    # ``Drifted`` asserted true, not ``Ready`` asserted false. A condition here
    # is a fact that holds, and ``resource_health`` reads only the ones that do
    # so a false Ready is not the opposite of a true one, it is a condition
    # nothing looks at.
    # Stamped, so the drift keeps the moment it was first seen however many
    # sweeps find it again: that is what lets a finding say what happened then.
    conditions = stamped(resource.conditions, [
        {
            "type": "Drifted",
            "status": True,
            "reason": "Drifted",
            "message": "The last sweep found "
            + "; ".join(_difference_phrase(field, asked, live) for field, asked, live in drift)
            + ".",
        }
    ])
    if conditions == resource.conditions:
        return
    resource.conditions = conditions
    resource.save(update_fields=["conditions"])


# Past this, a value is described rather than repeated: a policy document is
# three hundred lines, and a condition message is read in a table cell.
_QUOTABLE = 120


def _difference_phrase(field: str, asked: str, live: str) -> str:
    """One field's disagreement, said so a person can see what changed.

    Short values are quoted. Two JSON documents are compared by their top-level
    keys: what the live one has that the declaration does not, what it lacks,
    and what differs. Anything else long is sized, not pasted.
    """

    if len(asked) <= _QUOTABLE and len(live) <= _QUOTABLE:
        return f"{field} is {live or 'blank'}, where this asks for {asked or 'blank'}"
    try:
        wanted, found = json.loads(asked), json.loads(live)
    except ValueError:
        wanted = found = None
    if isinstance(wanted, dict) and isinstance(found, dict):
        added = sorted(set(found) - set(wanted))
        missing = sorted(set(wanted) - set(found))
        changed = sorted(key for key in set(found) & set(wanted) if found[key] != wanted[key])
        parts = [
            *(f"+ {key}" for key in added),
            *(f"- {key}" for key in missing),
            *(f"{key} changed" for key in changed),
        ]
        return f"{field} differs from what this asks for: {', '.join(parts) or 'layout only'}"
    return f"{field} differs from what this asks for ({len(live)} characters live, {len(asked)} declared)"


@transaction.atomic
def record_step_failures(
    payload: list[dict[str, Any]], *, principal: Principal, controller_id: str = ""
) -> dict[str, Any]:
    """Store the work one pass could not finish, against the connection it used.

    Reported at the end of a pass and written onto the rows the sweep wrote at
    the start of it, so a failure that happened after the sweep still lands on
    the right connection. Every connection this controller carries is written,
    including the ones with nothing to report: a pass that cleared a failure
    has to be able to say so, and only writing failures would leave the last
    one standing forever.
    """

    principal.require(Capability.MANAGE_INFRASTRUCTURE)
    by_ref: dict[str, list[dict[str, str]]] = {}
    for item in payload:
        subject = item["subject"].strip()
        step = item["step"].strip()
        # A failure naming no connection or no step has no row to stand on.
        if not subject or not step:
            continue
        by_ref.setdefault(subject, []).append(
            {"step": step[:200], "reason": item["reason"][:120]}
        )
    updated = 0
    for connection in ProviderConnection.objects.filter(controller_id=controller_id):
        failing = by_ref.get(connection.connection_ref, [])
        if connection.failing_steps == failing:
            continue
        connection.failing_steps = failing
        connection.save(update_fields=["failing_steps"])
        updated += 1
    return {"ok": True, "updated": updated}


@transaction.atomic
def record_connections(
    payload: list[dict[str, Any]], *, principal: Principal, controller_id: str = ""
) -> dict[str, Any]:
    """Store what one controller can currently reach, replacing its last answer.

    Scoped to the controller that reported it, so a connection this one no
    longer carries goes away without touching another controller's. A credential
    revoked in 1Password stops being offered on the next sweep, which is the
    whole point of holding this as an observation rather than as a list.
    """

    principal.require(Capability.MANAGE_INFRASTRUCTURE)
    observed_at = timezone.now()
    stored = []
    for connection in payload:
        connection_ref = connection["connection_ref"].strip()
        if not connection_ref:
            continue
        endpoint = connection["endpoint"][:500]
        if endpoint and endpoint_has_private_parts(endpoint):
            raise ValueError(
                f"Connection {connection_ref!r} endpoint contains private URL parts."
            )
        if connection.get("carried"):
            # Reported without being asked again, because HQ said its last
            # answer was recent and good. The probe's result and time are kept,
            # so the page says when it was last really checked; the report is
            # this pass's. A connection HQ has never seen answer cannot be
            # carried.
            carried = ProviderConnection.objects.filter(
                controller_id=controller_id, connection_ref=connection_ref
            ).update(
                provider=connection["provider"][:64],
                endpoint=endpoint,
                manages=connection["manages"],
                store=store_references(connection.get("store")),
                reported_at=observed_at,
            )
            if carried:
                stored.append(connection_ref)
                continue
        before = (
            ProviderConnection.objects.filter(
                controller_id=controller_id, connection_ref=connection_ref
            )
            .values_list("reachable", "probed", "manages")
            .first()
        )
        row, _ = ProviderConnection.objects.update_or_create(
            controller_id=controller_id,
            connection_ref=connection_ref,
            defaults={
                "provider": connection["provider"][:64],
                "endpoint": endpoint,
                "reaches": [name for name in connection["reaches"] if name],
                "reachable": connection["ok"],
                "probed": connection["probed"],
                "detail": connection["detail"][:500],
                # Why a probe failed is kept only for one that did.
                "failure": "" if connection["ok"] else connection.get("failure", ""),
                "manages": connection["manages"],
                "expires_at": parse_expiry(connection.get("expires_at")),
                "store": store_references(connection.get("store")),
                "observed_at": observed_at,
                "reported_at": observed_at,
            },
        )
        # The row carries when it was last checked; the audit log carries changes.
        if before is None or before[:2] != (row.reachable, row.probed):
            _record_probe(row)
        if (before[2] if before else False) != row.manages:
            _record_manages(row)
        stored.append(connection_ref)
    ProviderConnection.objects.filter(controller_id=controller_id).exclude(
        connection_ref__in=stored
    ).delete()
    return {
        "ok": True,
        "recorded": sorted(stored),
        "observed_at": observed_at.isoformat(),
    }


def _record_probe(row: ProviderConnection) -> None:
    """A routine event for a connection first seen, or whose probe outcome changed."""

    if not row.probed:
        outcome = "not probed"
    elif row.reachable:
        outcome = "reachable"
    else:
        outcome = "unreachable"
    record_event(
        action=AuditLog.Action.OBSERVED,
        obj=row,
        type_label=CONNECTION_AUDIT_TYPE,
        message=f"Probed, {outcome}",
        metadata={"controller_id": row.controller_id, "provider": row.provider},
        connection=row.connection_ref,
    )


def _record_manages(row: ProviderConnection) -> None:
    """Whether a connection may make records managed. Kept, never pruned."""

    record_event(
        action=AuditLog.Action.SETTINGS_CHANGED,
        obj=row,
        type_label=CONNECTION_AUDIT_TYPE,
        message="Allowed to manage its records" if row.manages else "Set to observe only",
        metadata={"controller_id": row.controller_id, "provider": row.provider},
        connection=row.connection_ref,
    )


def service_hostnames(kind: str, spec: dict[str, Any]) -> tuple[str, ...]:
    """The hostnames a spec claims, normalised.

    The same function the providers use for the service view, so a name here
    means exactly what "the same service" means everywhere else.
    """

    provider = PROVIDERS[kind]
    if provider.hostnames is None:
        return ()
    try:
        return tuple(
            sorted(normalized_hostname(name) for name in provider.hostnames(spec))
        )
    except (KeyError, TypeError, ValueError):
        return ()


def record_identity(kind: str, spec: dict[str, Any]) -> tuple[str, ...]:
    """What makes a live record and a declaration the same thing.

    Falls back to the hostnames, which suffices for a provider with one record
    per name. A provider that can hold several records
    for a single name says so itself (see ``ProviderSpec.identity``) because
    hostname identity would silently merge them and adopt whichever the provider
    listed first.
    """

    provider = PROVIDERS[kind]
    if provider.identity is not None:
        try:
            return tuple(provider.identity(spec))
        except (KeyError, TypeError, ValueError):
            return ()
    return service_hostnames(kind, spec)


def inventory_state() -> tuple[dict[str, Any], ...]:
    """Each provider's last sweep, for a surface that has to say how stale it is."""

    from .credential_sight import standing

    found = []
    # A kind no connection could read is not a reading of zero.
    for snapshot in ProviderInventory.objects.filter(connected=True):
        state, state_label = standing(snapshot)
        found.append(
            {
                "kind": snapshot.kind,
                "label": registry_label(snapshot.kind),
                "count": len(snapshot.records),
                "reachable": snapshot.reachable,
                "error": snapshot.error,
                "observed_at": snapshot.observed_at,
                "state": state,
                "state_label": state_label,
                "refused_parts": tuple(refused.phrase for refused in refused_parts(snapshot)),
            }
        )
    return tuple(found)
