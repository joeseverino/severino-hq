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
from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.utils import timezone

from control_plane.models import (
    ManagedResource,
    ProviderConnection,
    ProviderInventory,
)
from control_plane.observations import OBSERVATIONS
from control_plane.provider_adapters.contracts import REFUSALS
from control_plane.providers import OBSERVATION_KINDS, PROVIDERS, registry_label, service_facets
from core.audit import CONNECTION_AUDIT_TYPE, record_event
from core.models import AuditLog

from control_plane.names import normalized_hostname

from .contracts import endpoint_has_private_parts

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


@dataclass(frozen=True)
class Unmanaged:
    """One record a provider holds that no HQ declaration accounts for.

    ``identity`` is what makes it that record; ``hostnames`` is what it serves.
    They are the same for a rewrite or a proxy host and deliberately different
    for a DNS record, which may be one of nine on a single name and may serve
    nothing at all.
    """

    kind: str
    identity: tuple[str, ...]
    hostnames: tuple[str, ...]
    spec: dict[str, Any]
    observed_at: Any
    # False when the provider will not take this record on unasked: it stays
    # here, and a finding says so, until a person adopts or removes it.
    adoptable: bool = True
    # The connection that read it, when the record names one.
    connection_ref: str = ""
    # True unless it was read through a connection that manages. An observed
    # record is shown and never adopted. See ``application.adoption``.
    observed_only: bool = True

    @property
    def label(self) -> str:
        return registry_label(self.kind)

    @property
    def hostname(self) -> str:
        return self.hostnames[0] if self.hostnames else ""

    @property
    def token(self) -> str:
        """A short, stable handle for this exact record, safe to put in a URL.

        Derived rather than stored because nothing persists an unmanaged record
        it exists only in the last sweep. Hashed rather than joined because
        an identity contains a DNS value, and a TXT record's value is neither
        short nor URL-safe.
        """

        return record_token(self.kind, self.identity)

    @property
    def readout(self) -> tuple[tuple[str, str], ...]:
        """What this record does, described by its own provider.

        The listing template reached into ``spec.answer`` and ``spec.forward_host``
        directly, which is the one thing nothing outside a provider is allowed to
        do: an AdGuard record has neither of the fields a proxy host has, and the
        page failed the moment both kinds appeared on it. The provider already
        says how to describe itself.
        """

        provider = PROVIDERS[self.kind]
        if provider.readout is None:
            return ()
        try:
            rows = provider.readout(self.spec, {})
        except (KeyError, TypeError, ValueError):
            return ()
        return tuple(
            (label, str(desired)) for label, desired, _ in rows if desired
        )


@transaction.atomic
def record_inventory(
    payload: dict[str, Any], *, principal: Principal, controller_id: str = ""
) -> dict[str, Any]:
    """Store one controller sweep, replacing whatever the last one said.

    Replaced rather than merged: this describes a provider at a moment, and
    merging would keep records that have since been deleted, which is the one
    thing a staleness-aware cache must not do.

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
    for kind, report in sorted(payload.items()):
        if kind not in PROVIDERS and kind not in OBSERVATION_KINDS:
            # A controller ahead of this HQ. Ignored rather than rejected: the
            # rest of the sweep is still true, and refusing it would make a
            # controller upgrade take the whole inventory down.
            continue
        reached = bool(report.get("ok", True))
        connected = bool(report.get("connected", True))
        records = report.get("records") or []
        error = str(report.get("error", ""))
        refusal = str(report.get("refusal", "")) if not reached else ""
        # An unknown refusal is stored as none, so no remedy is offered for it.
        if refusal not in REFUSALS:
            refusal = ""
        observation = OBSERVATIONS.get(kind)
        if observation is not None:
            records, refused = observation.clean(records)
            if refused:
                error = error or (
                    f"{counted(refused, 'record')} did not match the {kind} schema."
                )
        seen = {"records": records, "observed_at": observed_at}
        ProviderInventory.objects.update_or_create(
            kind=kind,
            # An unreachable provider leaves the last sweep's records and the
            # moment it took them exactly where they were.
            defaults={
                "reachable": reached,
                "connected": connected,
                "error": error[:500],
                "refusal": refusal,
                "controller_id": controller_id,
                **(seen if reached else {}),
            },
            create_defaults={
                "reachable": reached,
                "connected": connected,
                "error": error[:500],
                "refusal": refusal,
                "controller_id": controller_id,
                **seen,
            },
        )
        stored.append(kind)

    # Adoption is not done here. A record in a domain HQ has been made
    # responsible for is HQ's, but which records those are is `zones`' to say,
    # and reaching for it from inside the sweep made the two modules import
    # each other. `application.sweep` composes the pair instead.
    return {
        "ok": True,
        "recorded": stored,
        "observed_at": observed_at.isoformat(),
    }


def confirm_observed(payload: dict[str, Any]) -> int:
    """Mark declarations the sweep just found still matching as observed.

    A declaration is "in sync" when what HQ asked for is what is there, and a
    sweep is HQ going and looking. Yet only a reconcile ever wrote that down,
    so a declaration nothing had changed sat reporting "never reported", and
    nothing queues a reconcile for a resource that has not drifted, so the
    first look never came. Whole services read as unverified while every part
    of them was running and had just been seen.

    Only where the spec still matches the live record. A declaration that has
    drifted is exactly the one a reconcile should visit, and quietly calling it
    observed would hide the difference this whole model exists to surface.
    """

    from django.utils import timezone

    seen = timezone.now()
    confirmed = 0
    for kind, report in payload.items():
        if kind not in PROVIDERS or not report.get("ok", True):
            continue
        live = {}
        for record in report.get("records") or []:
            spec = _spec_from_record(kind, record)
            if spec is not None:
                live[_identity(kind, spec)] = spec
        if not live:
            continue
        for resource in ManagedResource.objects.filter(kind=kind, enabled=True):
            found = live.get(_identity(kind, resource.spec))
            if found is None:
                continue
            drift = _differences(kind, resource.spec, found)
            if drift:
                _record_drift(resource, drift)
                continue
            resource.observed_generation = resource.generation
            resource.last_observed_at = seen
            resource.status = dict(found)
            resource.conditions = [
                {
                    "type": "Ready",
                    "status": True,
                    "reason": "Observed",
                    "message": "The last sweep found this exactly as declared.",
                }
            ]
            resource.save(
                update_fields=[
                    "observed_generation",
                    "last_observed_at",
                    "status",
                    "conditions",
                ]
            )
            confirmed += 1
    return confirmed


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

    The comparison rule above, stated once and returning what it saw rather than
    only whether it saw anything. Asking "do these match" and asking "how do
    these differ" with two implementations is how a page comes to report drift
    it cannot describe, or describe drift that is not there.
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
    read back: byte for byte the same but for the line endings. A tailnet
    policy sat drifted on that for a week, having been applied successfully and
    accepted by Tailscale seconds earlier.
    """

    text = str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n") if "\r" in text else text
    return _canonical_document(text)


def _canonical_document(text: str) -> str:
    """A JSON document reduced to what it says, so layout is not a difference.

    Line endings were only half of it. HQ stores the tailnet policy it applied
    minified, on one line, and Tailscale hands the same policy back
    pretty-printed across three hundred. Compared as text they never match, so
    the policy read "Drifted" from the moment it was applied, and because a
    drifted record is never stamped as observed, the kind then aged into "nothing
    has observed this for 12 days". Two alarms, both false, and a real change to
    the ACL would have looked exactly the same as either.

    Only a value that parses as a JSON object or array is touched; anything else,
    including a policy written as HuJSON with comments, is compared as the text
    it is, which is the old behaviour and errs towards reporting a difference.
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
    # nothing looks at, and the summary card went on saying "not observed" above
    # a table that described the drift in full.
    resource.conditions = [
        {
            "type": "Drifted",
            "status": True,
            "reason": "Drifted",
            "message": "The last sweep found "
            + "; ".join(
                f"{field} is {live or 'blank'}, where this asks for "
                f"{asked or 'blank'}"
                for field, asked, live in drift
            )
            + ".",
        }
    ]
    resource.save(update_fields=["conditions"])


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
        if not isinstance(item, dict):
            continue
        subject = str(item.get("subject", "")).strip()
        step = str(item.get("step", "")).strip()
        if not subject or not step:
            continue
        by_ref.setdefault(subject, []).append(
            {"step": step[:200], "reason": str(item.get("reason", ""))[:120]}
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
        connection_ref = str(connection.get("connection_ref", "")).strip()
        if not connection_ref:
            continue
        endpoint = str(connection.get("endpoint", ""))[:500]
        if endpoint and endpoint_has_private_parts(endpoint):
            raise ValueError(
                f"Connection {connection_ref!r} endpoint contains private URL parts."
            )
        if connection.get("carried"):
            # Reported without being asked again, because HQ said its last
            # answer was recent and good. Kept as it was (result and the time
            # it was taken) so the page says when it was last really checked.
            # A connection HQ has never seen answer cannot be carried.
            carried = ProviderConnection.objects.filter(
                controller_id=controller_id, connection_ref=connection_ref
            ).update(
                provider=str(connection.get("provider", ""))[:64],
                endpoint=endpoint,
                manages=connection.get("manages") is True,
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
                "provider": str(connection.get("provider", ""))[:64],
                "endpoint": endpoint,
                "reaches": [
                    str(name) for name in connection.get("reaches") or [] if name
                ],
                "reachable": bool(connection.get("ok", True)),
                "probed": bool(connection.get("probed", True)),
                "detail": str(connection.get("detail", ""))[:500],
                # Only an explicit true manages; anything else observes.
                "manages": connection.get("manages") is True,
                "observed_at": observed_at,
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
        message="Manages" if row.manages else "Observes only",
        metadata={"controller_id": row.controller_id, "provider": row.provider},
        connection=row.connection_ref,
    )


def _service_hostnames(kind: str, spec: dict[str, Any]) -> tuple[str, ...]:
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


def _identity(kind: str, spec: dict[str, Any]) -> tuple[str, ...]:
    """What makes a live record and a declaration the same thing.

    Falls back to the hostnames, which is what identity meant when every
    provider had one record per name. A provider that can hold several records
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
    return _service_hostnames(kind, spec)


def unmanaged() -> tuple[Unmanaged, ...]:
    """Records a provider holds that no enabled declaration accounts for.

    Matched on hostname rather than on any provider id, because that is how the
    reconcilers find their own records. A declaration and a live record with the
    same hostnames are the same thing by the only definition that governs what
    actually happens.
    """

    from .infrastructure import enabled_resources

    declared: dict[str, set[tuple[str, ...]]] = {}
    for resource in enabled_resources():
        if resource.kind not in PROVIDERS:
            continue
        declared.setdefault(resource.kind, set()).add(
            _identity(resource.kind, resource.spec)
        )

    from .adoption import manages_through

    manages = manages_through()
    found: list[Unmanaged] = []
    for snapshot in ProviderInventory.objects.all():
        provider = PROVIDERS.get(snapshot.kind)
        if provider is None or provider.from_record is None:
            continue
        known = declared.get(snapshot.kind, set())
        for record in snapshot.records:
            try:
                spec = provider.from_record(record)
            except (KeyError, TypeError, ValueError):
                continue
            identity = _identity(snapshot.kind, spec)
            if not identity or identity in known:
                continue
            connection_ref = str(record.get("connection_ref", "") or "")
            found.append(
                Unmanaged(
                    kind=snapshot.kind,
                    identity=identity,
                    hostnames=_service_hostnames(snapshot.kind, spec),
                    spec=spec,
                    observed_at=snapshot.observed_at,
                    adoptable=provider.adopts is None or provider.adopts(record),
                    connection_ref=connection_ref,
                    observed_only=not manages(snapshot.kind, connection_ref),
                )
            )
    return tuple(sorted(found, key=lambda item: (item.identity, item.kind)))


@dataclass(frozen=True)
class UnmanagedService:
    """Every unmanaged record sharing one hostname, seen as one thing.

    Grouped because a hostname is the unit an operator thinks in, and because
    the managed table beside this one is already per-hostname. Listed per record
    instead, one service appeared as two adjacent rows with the same name, and
    onboarding it took two clicks: the page taught two different shapes for
    the same idea.
    """

    hostname: str
    items: tuple[Unmanaged, ...]

    @property
    def observed_only(self) -> bool:
        """Whether every record behind the name is read only to observe."""

        return all(item.observed_only for item in self.items)

    @property
    def observed_at(self):
        return max(item.observed_at for item in self.items)

    @property
    def facets(self) -> tuple[tuple[str, str, str], ...]:
        """``(id, label, value)`` per facet, lining up with the managed table.

        The value only. Each readout row carries its own label: "Answers with",
        "Forwards to", which is right on a detail card that has no column
        headings, and pure noise in a table whose column already says DNS. The
        secondary rows go the same way: what a list is for is scanning where a
        name points, and the rest is one click away.
        """

        by_facet = {
            PROVIDERS[item.kind].facet: item.readout
            for item in self.items
            if PROVIDERS[item.kind].facet
        }
        return tuple(
            (
                facet_id,
                label,
                by_facet.get(facet_id, (("", ""),))[0][1],
            )
            for facet_id, label in service_facets()
        )


def unmanaged_services() -> tuple[UnmanagedService, ...]:
    """Unmanaged records grouped by the service they serve.

    Records that serve no hostname are deliberately absent. A DMARC policy and a
    CAA record are real, unmanaged and worth adopting, but they are not services
    and grouping them here would file every one of them under a service whose
    name is the empty string.
    """

    grouped: dict[str, list[Unmanaged]] = {}
    for item in unmanaged():
        if not item.hostname:
            continue
        grouped.setdefault(item.hostname, []).append(item)
    return tuple(
        UnmanagedService(hostname=hostname, items=tuple(items))
        for hostname, items in sorted(grouped.items())
    )


def find_unmanaged(
    kind: str, hostname: str = "", *, token: str = ""
) -> Unmanaged | None:
    """One unmanaged record, found by exact identity or by the name it serves.

    Both, because both questions are asked. "Adopt this service" means every
    record behind a hostname; "adopt this record" means one row of a zone, which
    may share its hostname with eight others and may serve nothing at all.
    """

    candidates = [item for item in unmanaged() if item.kind == kind]
    if token:
        return next((item for item in candidates if item.token == token), None)
    wanted = normalized_hostname(hostname)
    return next((item for item in candidates if wanted in item.hostnames), None)


@dataclass(frozen=True)
class AdoptServiceCommand:
    hostname: str


@transaction.atomic
def adopt_service(
    command: AdoptServiceCommand,
    *,
    principal: Principal,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Adopt every unmanaged record behind one hostname, or none of them.

    A hostname is the unit an operator is thinking about: its DNS record and
    the proxy host in front of it are one decision, not two. Atomic because a
    half-adopted service is worse than an unadopted one: HQ would manage the
    name's ingress while its DNS answer stayed outside, and the service page
    would show a gap that is not really there.
    """

    del expected_updated_at
    from .infrastructure import NotFoundError

    found = next(
        (
            service
            for service in unmanaged_services()
            if service.hostname == normalized_hostname(command.hostname)
        ),
        None,
    )
    if found is None:
        raise NotFoundError(
            f"Nothing unmanaged was last seen for {command.hostname!r}. It may "
            "have been adopted already, or removed at the provider."
        )
    from .infrastructure import PolicyError

    writable = [item for item in found.items if not item.observed_only]
    if not writable:
        raise PolicyError(
            f"{found.hostname} is read through connections that only observe."
        )
    adopted = [
        adopt(
            # By token, not by hostname: a service may be served by several
            # records of one kind, and adopting by name would adopt the first
            # one repeatedly and silently skip the rest.
            AdoptCommand(kind=item.kind, token=item.token),
            principal=principal,
        )["resource"]["key"]
        for item in writable
    ]
    return {"ok": True, "hostname": found.hostname, "adopted": adopted}


@dataclass(frozen=True)
class AdoptCommand:
    kind: str
    hostname: str = ""
    key: str = ""
    # Set when adopting one specific record rather than everything a hostname
    # answers with. Takes precedence: it identifies exactly one row, where a
    # hostname may match several.
    token: str = ""


def adopt(
    command: AdoptCommand,
    *,
    principal: Principal,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Bring a record the provider already holds under HQ's management.

    The spec comes from the live record, so adopting asserts nothing new: the
    resource is created already in sync with the world, and the first
    reconciliation is a no-op. That is the whole safety argument, and it is why
    this reads the record again at adoption time rather than trusting a spec
    posted by a browser: a form could carry a stale or edited copy, and the
    point of adopting is to capture what is actually there.

    Routed through ``save_managed_resource`` rather than creating a row, so the
    capability check, the spec validation, the fingerprint and the audit record
    are the ones every other write already uses.
    """

    del expected_updated_at
    from .adoption import let_in
    from .infrastructure import (
        ManagedResourceCommand,
        NotFoundError,
        PolicyError,
        save_managed_resource,
    )

    found = find_unmanaged(command.kind, command.hostname, token=command.token)
    if found is None:
        subject = command.hostname or command.token or "that record"
        raise NotFoundError(
            f"No unmanaged {command.kind} was last seen for {subject!r}. "
            "It may have been adopted already, or removed at the provider."
        )
    if found.observed_only:
        raise PolicyError(
            f"This {found.label.lower()} is read through a connection that only "
            "observes. Set manages on the connection to adopt it."
        )
    result = save_managed_resource(
        ManagedResourceCommand(
            key=command.key or suggested_key(found),
            kind=found.kind,
            spec=found.spec,
            enabled=True,
        ),
        principal=principal,
        copied_from_live=True,
    )
    _record_as_observed(result.get("resource", {}).get("key", ""), found)
    # Adopting is the operator managing it again.
    let_in(found.kind, found.token)
    return result


def _record_as_observed(key: str, found: "Unmanaged") -> None:
    """Mark an adopted resource as seen, because it just was.

    Everything else here is born unobserved and waits for a controller to go
    and look, which is right: a declaration somebody typed is a claim about a
    world nobody has checked. Adoption is the one case where that is false. The
    spec was read from the live record moments ago, so a resource created from
    it is in sync by construction: that is the entire safety argument for
    adopting rather than declaring.

    Left unmarked, it says "never reported" forever: nothing queues a
    reconcile for a resource that has not drifted, so the first look never
    comes, and a service assembled from it reads as incomplete while every part
    of it is running.
    """

    from django.utils import timezone

    from control_plane.models import ManagedResource

    resource = ManagedResource.objects.filter(key=key).first()
    if resource is None:
        return
    resource.observed_generation = resource.generation
    resource.last_observed_at = timezone.now()
    # What was found, which for an adopted resource is what was declared.
    resource.status = dict(found.spec)
    resource.conditions = [
        {
            "type": "Ready",
            "status": True,
            "reason": "Adopted",
            "message": "Adopted from what the provider was holding.",
        }
    ]
    resource.save(
        update_fields=[
            "observed_generation",
            "last_observed_at",
            "status",
            "conditions",
        ]
    )


def suggested_key(item: Unmanaged) -> str:
    """A free key an operator would recognise on a list of declarations."""

    from .infrastructure import suggest_key

    return suggest_key(item.kind, item.spec)


def inventory_state() -> tuple[dict[str, Any], ...]:
    """Each provider's last sweep, for a surface that has to say how stale it is."""

    return tuple(
        {
            "kind": snapshot.kind,
            "label": registry_label(snapshot.kind),
            "count": len(snapshot.records),
            "reachable": snapshot.reachable,
            "error": snapshot.error,
            "observed_at": snapshot.observed_at,
        }
        # A kind no connection could read is not a reading of zero.
        for snapshot in ProviderInventory.objects.filter(connected=True)
    )


def adopt_discovered(kind: str, *, principal) -> dict[str, Any]:
    """Take on every record of one kind that no declaration accounts for.

    Only records read through a connection that manages, and none an operator
    said HQ does not manage. The connection's ``manages`` is the decision; a
    connection that only observes adopts nothing.
    """

    from django.core.exceptions import ValidationError

    from .adoption import kept_out
    from .infrastructure import NotFoundError, PolicyError

    excluded = kept_out()
    adopted: list[str] = []
    for item in unmanaged():
        if item.kind != kind or not item.adoptable or item.observed_only:
            continue
        if (item.kind, item.token) in excluded:
            continue
        try:
            result = adopt(
                AdoptCommand(kind=item.kind, token=item.token), principal=principal
            )
        except (NotFoundError, PolicyError, ValidationError, ValueError):
            # One record that cannot be adopted must not stop the rest. The
            # next sweep tries again, so this closes itself rather than needing
            # anybody to notice.
            continue
        adopted.append(result.get("resource", {}).get("key", ""))
    return {"adopted": [key for key in adopted if key]}
