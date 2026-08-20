"""What the providers hold, and which of it HQ does not manage.

The controller fetches every rewrite and every proxy host on each pass and, for
years, kept exactly one record per pass -- the one it had been asked to
reconcile. Everything else was discarded at the point where it had already been
paid for. So HQ knew about the resources it had created and nothing about the
thirteen that were simply there.

This records the rest. It is a cache and stays one: nothing reconciles from it,
and HQ never becomes a second copy of AdGuard. What it buys is the difference
between a registry and a console -- an operator can see what exists, and adopt
what HQ should be looking after.

Adoption is safe because the spec is read back out of the live record through
the provider's own ``from_record``. The declaration starts equal to the world,
so the first reconciliation after adopting changes nothing. Anything else would
mean adopting a host quietly reset it to HQ's defaults, which is precisely the
bug that made HSTS switch itself off.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.utils import timezone

from control_plane.models import ManagedResource, ProviderInventory
from control_plane.providers import PROVIDERS, service_facets

from .security import Capability, Principal


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

    @property
    def label(self) -> str:
        return PROVIDERS[self.kind].label or self.kind

    @property
    def hostname(self) -> str:
        return self.hostnames[0] if self.hostnames else ""

    @property
    def token(self) -> str:
        """A short, stable handle for this exact record, safe to put in a URL.

        Derived rather than stored because nothing persists an unmanaged record
        -- it exists only in the last sweep. Hashed rather than joined because
        an identity contains a DNS value, and a TXT record's value is neither
        short nor URL-safe.
        """

        joined = "\x1f".join((self.kind, *self.identity))
        return hashlib.sha256(joined.encode()).hexdigest()[:16]

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
    """

    principal.require(Capability.MANAGE_INFRASTRUCTURE)
    observed_at = timezone.now()
    stored = []
    for kind, report in sorted(payload.items()):
        if kind not in PROVIDERS:
            # A controller ahead of this HQ. Ignored rather than rejected: the
            # rest of the sweep is still true, and refusing it would make a
            # controller upgrade take the whole inventory down.
            continue
        ProviderInventory.objects.update_or_create(
            kind=kind,
            defaults={
                "records": report.get("records") or [],
                "reachable": bool(report.get("ok", True)),
                "error": str(report.get("error", ""))[:500],
                "observed_at": observed_at,
                "controller_id": controller_id,
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
            sorted(name.strip().lower().rstrip(".") for name in provider.hostnames(spec))
        )
    except (KeyError, TypeError, ValueError):
        return ()


def _identity(kind: str, spec: dict[str, Any]) -> tuple[str, ...]:
    """What makes a live record and a declaration the same thing.

    Falls back to the hostnames, which is what identity meant when every
    provider had one record per name. A provider that can hold several records
    for a single name says so itself -- see ``ProviderSpec.identity`` -- because
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

    declared: dict[str, set[tuple[str, ...]]] = {}
    for resource in ManagedResource.objects.filter(enabled=True):
        if resource.kind not in PROVIDERS:
            continue
        declared.setdefault(resource.kind, set()).add(
            _identity(resource.kind, resource.spec)
        )

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
            found.append(
                Unmanaged(
                    kind=snapshot.kind,
                    identity=identity,
                    hostnames=_service_hostnames(snapshot.kind, spec),
                    spec=spec,
                    observed_at=snapshot.observed_at,
                )
            )
    return tuple(sorted(found, key=lambda item: (item.identity, item.kind)))


@dataclass(frozen=True)
class UnmanagedService:
    """Every unmanaged record sharing one hostname, seen as one thing.

    Grouped because a hostname is the unit an operator thinks in, and because
    the managed table beside this one is already per-hostname. Listed per record
    instead, one service appeared as two adjacent rows with the same name, and
    onboarding it took two clicks -- the page taught two different shapes for
    the same idea.
    """

    hostname: str
    items: tuple[Unmanaged, ...]

    @property
    def observed_at(self):
        return max(item.observed_at for item in self.items)

    @property
    def facets(self) -> tuple[tuple[str, str, str], ...]:
        """``(id, label, value)`` per facet, lining up with the managed table.

        The value only. Each readout row carries its own label -- "Answers with",
        "Forwards to" -- which is right on a detail card that has no column
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
    wanted = hostname.strip().lower().rstrip(".")
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

    A hostname is the unit an operator is thinking about -- its DNS record and
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
            if service.hostname == command.hostname.strip().lower().rstrip(".")
        ),
        None,
    )
    if found is None:
        raise NotFoundError(
            f"Nothing unmanaged was last seen for {command.hostname!r}. It may "
            "have been adopted already, or removed at the provider."
        )
    adopted = [
        adopt(
            # By token, not by hostname: a service may be served by several
            # records of one kind, and adopting by name would adopt the first
            # one repeatedly and silently skip the rest.
            AdoptCommand(kind=item.kind, token=item.token),
            principal=principal,
        )["resource"]["key"]
        for item in found.items
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
    posted by a browser -- a form could carry a stale or edited copy, and the
    point of adopting is to capture what is actually there.

    Routed through ``save_managed_resource`` rather than creating a row, so the
    capability check, the spec validation, the fingerprint and the audit record
    are the ones every other write already uses.
    """

    del expected_updated_at
    from .infrastructure import ManagedResourceCommand, NotFoundError, save_managed_resource

    found = find_unmanaged(command.kind, command.hostname, token=command.token)
    if found is None:
        subject = command.hostname or command.token or "that record"
        raise NotFoundError(
            f"No unmanaged {command.kind} was last seen for {subject!r}. "
            "It may have been adopted already, or removed at the provider."
        )
    return save_managed_resource(
        ManagedResourceCommand(
            key=command.key or suggested_key(found),
            kind=found.kind,
            spec=found.spec,
            enabled=True,
        ),
        principal=principal,
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
            "label": (PROVIDERS[snapshot.kind].label or snapshot.kind)
            if snapshot.kind in PROVIDERS
            else snapshot.kind,
            "count": len(snapshot.records),
            "reachable": snapshot.reachable,
            "error": snapshot.error,
            "observed_at": snapshot.observed_at,
        }
        for snapshot in ProviderInventory.objects.all()
    )
