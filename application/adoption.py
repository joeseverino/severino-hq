"""Taking on what HQ found: which records it may adopt, and adopting them.

``unmanaged`` lists live records no declaration claims; ``adopt`` turns one
into a declaration, and ``adopt_service`` does so for every record a name has.

A record is adopted only through a connection that manages: one whose item
declares ``manages`` (rendered as ``<PREFIX>_MANAGES=1``). Every other
connection observes. What it reads is shown as observed and never declared.

A declaration an operator stops managing leaves a ``NotManaged`` row. Adoption
skips that record until an operator manages it again, which clears the row.
Both are audited through the model's registration.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from django.db import transaction

from control_plane.models import NotManaged, ProviderConnection, ProviderInventory
from control_plane.names import normalized_hostname
from control_plane.providers import PROVIDERS, registry_label, service_facets

from .conditions import stamped
from .inventory import record_identity, record_token, service_hostnames
from .security import Principal


OBSERVES_ONLY = "Its connection only observes. Set manages on the connection to act."


def manages_through(
    connections: Iterable[ProviderConnection] | None = None,
) -> Callable[[str, str], bool]:
    """A test of whether a record of ``kind`` read through ``ref`` may be adopted.

    A record naming its connection needs that connection to manage. A record
    naming none needs every connection of the kind's providers to manage, and
    at least one to exist: which of them read it is not known.
    """

    # Read on first use, so a caller with nothing to ask costs no query.
    loaded: list[dict[str, list[tuple[str, bool]]]] = []

    def by_provider() -> dict[str, list[tuple[str, bool]]]:
        if not loaded:
            from .connections import connection_rows

            rows = connection_rows() if connections is None else connections
            found: dict[str, list[tuple[str, bool]]] = {}
            for row in rows:
                found.setdefault(row.provider, []).append(
                    (row.connection_ref, row.manages)
                )
            loaded.append(found)
        return loaded[0]

    def manages(kind: str, connection_ref: str = "") -> bool:
        known = by_provider()
        if connection_ref:
            return any(
                ref == connection_ref and flag
                for rows in known.values()
                for ref, flag in rows
            )
        provider = PROVIDERS.get(kind)
        if provider is None:
            return False
        found = [
            flag
            for name in provider.connection_providers
            for _, flag in known.get(name, ())
        ]
        return bool(found) and all(found)

    return manages


def observes_only(kind: str, spec, manages=None) -> bool:
    """Whether a declaration of ``kind`` acts through a connection that only observes.

    ``manages`` is a ``manages_through()`` test, shared by a caller asking about
    many declarations; left as None it is built here. A kind that acts through
    no connection, or never acts (declaration-only), never observes only.
    """

    provider = PROVIDERS.get(kind)
    if provider is None or not provider.connection_providers or provider.declaration_only:
        return False
    test = manages or manages_through()
    return not test(kind, str((spec or {}).get("connection_ref", "")))


def kept_out() -> frozenset[tuple[str, str]]:
    """``(kind, token)`` for every record an operator said HQ does not manage."""

    return frozenset(NotManaged.objects.values_list("kind", "token"))


def keep_out(kind: str, token: str, label: str, *, principal) -> None:
    """Record that HQ does not manage this record. Idempotent."""

    NotManaged.objects.get_or_create(
        kind=kind,
        token=token,
        defaults={
            "label": label[:300],
            "actor": str(getattr(principal, "actor", "") or "")[:160],
        },
    )


def keep_out_declaration(kind: str, spec: dict, *, principal) -> None:
    """Keep out the record a forgotten declaration described.

    Only kinds a sweep can adopt; anything else has nothing to re-adopt.
    """

    provider = PROVIDERS.get(kind)
    if provider is None or provider.from_record is None:
        return
    identity = record_identity(kind, spec)
    if not identity:
        return
    keep_out(
        kind,
        record_token(kind, identity),
        " ".join(str(part) for part in identity),
        principal=principal,
    )


def let_in(kind: str, token: str) -> None:
    """Clear an operator's earlier choice, so the record is managed again."""

    for row in NotManaged.objects.filter(kind=kind, token=token):
        # Deleted per row so the audit signal records each.
        row.delete()


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

        Nothing outside a provider reads its spec fields: record kinds differ in
        shape, so each provider says how to describe itself.
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
            record_identity(resource.kind, resource.spec)
        )

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
            identity = record_identity(snapshot.kind, spec)
            if not identity or identity in known:
                continue
            connection_ref = str(record.get("connection_ref", "") or "")
            found.append(
                Unmanaged(
                    kind=snapshot.kind,
                    identity=identity,
                    hostnames=service_hostnames(snapshot.kind, spec),
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
    the managed table beside this one is already per-hostname: one service is
    one row and one adoption.
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
            if PROVIDERS[item.kind].facet and item.readout
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
    resource.conditions = stamped(resource.conditions, [
        {
            "type": "Ready",
            "status": True,
            "reason": "Adopted",
            "message": "Adopted from what the provider was holding.",
        }
    ])
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


def adopt_discovered(kind: str, *, principal) -> dict[str, Any]:
    """Take on every record of one kind that no declaration accounts for.

    Only records read through a connection that manages, and none an operator
    said HQ does not manage. The connection's ``manages`` is the decision; a
    connection that only observes adopts nothing.
    """

    from django.core.exceptions import ValidationError

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
